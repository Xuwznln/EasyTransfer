"""Extended TUS client for EasyTransfer."""

import os
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin

from tusclient.client import TusClient
from tusclient.uploader import Uploader

from etransfer.common.constants import AUTH_HEADER, DEFAULT_CHUNK_SIZE, TUS_VERSION
from etransfer.common.models import FileInfo, ServerInfo


class EasyTransferClient(TusClient):
    """Extended TUS client with additional features.

    Adds support for:
    - API token authentication
    - Server info queries
    - File listing
    - Best endpoint selection based on traffic
    """

    def __init__(
        self,
        server_url: str,
        token: Optional[str] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        **kwargs,
    ):
        """Initialize EasyTransfer client.

        Args:
            server_url: Server base URL
            token: API authentication token
            chunk_size: Upload chunk size
            **kwargs: Additional TusClient arguments
        """
        # TUS endpoint
        tus_url = urljoin(server_url.rstrip("/") + "/", "tus")

        # Set up headers
        headers = kwargs.pop("headers", {})
        if token:
            headers[AUTH_HEADER] = token
        headers["Tus-Resumable"] = TUS_VERSION

        super().__init__(tus_url, headers=headers, **kwargs)

        self.server_url = server_url.rstrip("/")
        self.token = token
        self.chunk_size = chunk_size
        self._http_client: Optional[Any] = None

    def _get_http_client(self):
        """Get or create HTTP client for API calls."""
        if self._http_client is None:
            import httpx

            headers = {}
            if self.token:
                headers[AUTH_HEADER] = self.token

            self._http_client = httpx.Client(
                base_url=self.server_url,
                headers=headers,
                timeout=30.0,
            )
        return self._http_client

    def close(self):
        """Close HTTP client."""
        if self._http_client:
            self._http_client.close()
            self._http_client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def get_server_info(self) -> ServerInfo:
        """Get server information.

        Returns:
            ServerInfo with server capabilities and status
        """
        client = self._get_http_client()
        response = client.get("/api/info")
        response.raise_for_status()
        return ServerInfo(**response.json())

    def list_files(
        self,
        page: int = 1,
        page_size: int = 20,
        include_partial: bool = True,
    ) -> list[FileInfo]:
        """List files on server.

        Args:
            page: Page number
            page_size: Items per page
            include_partial: Include partially uploaded files

        Returns:
            List of FileInfo objects
        """
        client = self._get_http_client()
        response = client.get(
            "/api/files",
            params={
                "page": page,
                "page_size": page_size,
                "include_partial": include_partial,
            },
        )
        response.raise_for_status()
        data = response.json()
        return [FileInfo(**f) for f in data.get("files", [])]

    def get_file_info(self, file_id: str) -> FileInfo:
        """Get information about a specific file.

        Args:
            file_id: File identifier

        Returns:
            FileInfo object
        """
        client = self._get_http_client()
        response = client.get(f"/api/files/{file_id}")
        response.raise_for_status()
        return FileInfo(**response.json())

    def select_best_endpoint(self, for_upload: bool = True) -> str:
        """Select the best server endpoint based on traffic load.

        Uses the /api/endpoints endpoint to get load-balanced recommendations.

        Args:
            for_upload: If True, select best for upload; else for download

        Returns:
            Best endpoint URL
        """
        try:
            client = self._get_http_client()
            response = client.get("/api/endpoints")
            response.raise_for_status()
            data = response.json()

            if for_upload and data.get("best_for_upload"):
                return data["best_for_upload"]
            elif not for_upload and data.get("best_for_download"):
                return data["best_for_download"]

            # Fallback to manual selection
            endpoints = data.get("endpoints", [])
            if not endpoints:
                return self.server_url

            # Find endpoint with lowest load
            key = "upload_load_percent" if for_upload else "download_load_percent"
            available = [e for e in endpoints if e.get("is_available", True)]
            if not available:
                return self.server_url

            best = min(available, key=lambda x: x.get(key, 100))
            return best.get("url", self.server_url)

        except Exception:
            return self.server_url

    def get_endpoints(self) -> dict:
        """Get all available endpoints with their load status.

        Returns:
            Dict with endpoints info and recommendations
        """
        client = self._get_http_client()
        response = client.get("/api/endpoints")
        response.raise_for_status()
        return response.json()

    def get_traffic(self) -> dict:
        """Get real-time traffic information.

        Returns:
            Dict with traffic stats for all interfaces
        """
        client = self._get_http_client()
        response = client.get("/api/traffic")
        response.raise_for_status()
        return response.json()

    def get_storage_status(self) -> dict:
        """Get server storage quota and usage information.

        Returns:
            Dict with used, max, available, usage_percent, is_full, etc.
        """
        client = self._get_http_client()
        response = client.get("/api/storage")
        response.raise_for_status()
        return response.json()

    def test_endpoint_connectivity(
        self,
        endpoint_url: str,
        timeout: float = 5.0,
    ) -> dict:
        """Test connectivity to a specific endpoint.

        Args:
            endpoint_url: Full URL of endpoint to test
            timeout: Request timeout

        Returns:
            Dict with connectivity test results:
            - reachable: bool
            - latency_ms: float (if reachable)
            - error: str (if not reachable)
        """
        import time

        import httpx

        try:
            headers = {}
            if self.token:
                headers[AUTH_HEADER] = self.token

            start = time.perf_counter()
            with httpx.Client(timeout=timeout) as test_client:
                response = test_client.get(
                    f"{endpoint_url.rstrip('/')}/api/health",
                    headers=headers,
                )
                latency = (time.perf_counter() - start) * 1000  # ms

                if response.status_code == 200:
                    return {
                        "reachable": True,
                        "latency_ms": round(latency, 2),
                        "status": response.json().get("status", "unknown"),
                    }
                else:
                    return {
                        "reachable": False,
                        "error": f"HTTP {response.status_code}",
                    }
        except Exception as e:
            return {
                "reachable": False,
                "error": str(e),
            }

    def test_all_endpoints(self, timeout: float = 5.0) -> dict:
        """Test connectivity to all available endpoints.

        Fetches endpoint list from server and tests each one.

        Args:
            timeout: Request timeout per endpoint

        Returns:
            Dict with:
            - endpoints: List of endpoint test results
            - best_reachable: URL of best reachable endpoint (lowest latency)
        """
        import concurrent.futures

        try:
            endpoints_data = self.get_endpoints()
        except Exception as e:
            return {
                "endpoints": [],
                "best_reachable": None,
                "error": f"Failed to get endpoints: {e}",
            }

        endpoints = endpoints_data.get("endpoints", [])
        results = []

        # Test endpoints in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            future_to_endpoint = {
                executor.submit(
                    self.test_endpoint_connectivity,
                    ep["url"],
                    timeout,
                ): ep
                for ep in endpoints
            }

            for future in concurrent.futures.as_completed(future_to_endpoint):
                ep = future_to_endpoint[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {"reachable": False, "error": str(e)}

                results.append(
                    {
                        "ip_address": ep["ip_address"],
                        "url": ep["url"],
                        "interface": ep.get("interface", "unknown"),
                        "upload_load_percent": ep.get("upload_load_percent", 0),
                        "download_load_percent": ep.get("download_load_percent", 0),
                        **result,
                    }
                )

        # Find best reachable endpoint (lowest latency)
        reachable = [r for r in results if r.get("reachable")]
        best = None
        if reachable:
            best = min(reachable, key=lambda x: x.get("latency_ms", float("inf")))

        return {
            "endpoints": results,
            "best_reachable": best["url"] if best else None,
            "best_latency_ms": best["latency_ms"] if best else None,
            "reachable_count": len(reachable),
            "total_count": len(results),
        }

    def select_best_reachable_endpoint(
        self,
        for_upload: bool = True,
        prefer_low_latency: bool = True,
        timeout: float = 5.0,
    ) -> str:
        """Select best reachable endpoint considering latency and load.

        Args:
            for_upload: If True, consider upload load; else download load
            prefer_low_latency: Prefer low latency over low load
            timeout: Connectivity test timeout

        Returns:
            Best endpoint URL
        """
        test_results = self.test_all_endpoints(timeout)
        reachable = [r for r in test_results["endpoints"] if r.get("reachable")]

        if not reachable:
            return self.server_url

        # Sort by preference
        load_key = "upload_load_percent" if for_upload else "download_load_percent"

        if prefer_low_latency:
            # Primary sort: latency, secondary: load
            reachable.sort(key=lambda x: (x.get("latency_ms", 999), x.get(load_key, 100)))
        else:
            # Primary sort: load, secondary: latency
            reachable.sort(key=lambda x: (x.get(load_key, 100), x.get("latency_ms", 999)))

        return reachable[0]["url"]

    def create_uploader(
        self,
        file_path: str,
        metadata: Optional[dict] = None,
        chunk_size: Optional[int] = None,
        retries: int = 3,
        retry_delay: float = 1.0,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        retention: Optional[str] = None,
        retention_ttl: Optional[int] = None,
        **kwargs,
    ) -> "EasyTransferUploader":
        """Create an uploader for a file.

        Args:
            file_path: Path to file to upload
            metadata: Additional metadata
            chunk_size: Override chunk size
            retries: Number of retry attempts
            retry_delay: Delay between retries
            progress_callback: Callback for progress updates
            retention: Retention policy (permanent/download_once/ttl).
                       If None, server default applies.
            retention_ttl: TTL in seconds (only for retention='ttl').
                           If None, server default applies.
            **kwargs: Additional uploader arguments

        Returns:
            EasyTransferUploader instance
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # Build metadata
        upload_metadata = {
            "filename": file_path.name,
        }

        # Try to determine MIME type
        import mimetypes

        mime_type, _ = mimetypes.guess_type(str(file_path))
        if mime_type:
            upload_metadata["filetype"] = mime_type

        # Add retention policy to metadata
        if retention:
            upload_metadata["retention"] = retention
        if retention_ttl is not None:
            upload_metadata["retention_ttl"] = str(retention_ttl)

        if metadata:
            upload_metadata.update(metadata)

        return EasyTransferUploader(
            client=self,
            file_path=str(file_path),
            file_size=file_path.stat().st_size,
            metadata=upload_metadata,
            chunk_size=chunk_size or self.chunk_size,
            retries=retries,
            retry_delay=retry_delay,
            progress_callback=progress_callback,
            **kwargs,
        )


class EasyTransferUploader(Uploader):
    """Extended TUS uploader with additional features.

    Adds support for:
    - Progress callbacks
    - Better retry handling
    - Local resume state
    """

    def __init__(
        self,
        client: EasyTransferClient,
        file_path: str,
        file_size: int,
        metadata: dict,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        retries: int = 3,
        retry_delay: float = 1.0,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        **kwargs,
    ):
        """Initialize uploader.

        Args:
            client: EasyTransfer client
            file_path: Path to file
            file_size: File size in bytes
            metadata: Upload metadata
            chunk_size: Chunk size
            retries: Number of retries
            retry_delay: Delay between retries
            progress_callback: Progress callback (uploaded_bytes, total_bytes)
            **kwargs: Additional arguments
        """
        # Don't pass url to let tusclient create new upload via POST
        super().__init__(
            file_path=file_path,
            client=client,
            chunk_size=chunk_size,
            metadata=metadata,
            retries=retries,
            retry_delay=int(retry_delay),
            **kwargs,
        )

        self.file_size = file_size
        self.progress_callback = progress_callback
        self._uploaded_bytes = 0

    @property
    def uploaded_bytes(self) -> int:
        """Get number of uploaded bytes."""
        return self._uploaded_bytes

    @property
    def progress(self) -> float:
        """Get upload progress percentage."""
        if self.file_size == 0:
            return 100.0
        return (self._uploaded_bytes / self.file_size) * 100

    def upload_chunk(self):
        """Upload a single chunk with progress tracking."""
        result = super().upload_chunk()

        # Update progress
        self._uploaded_bytes = self.offset
        if self.progress_callback:
            self.progress_callback(self._uploaded_bytes, self.file_size)

        return result

    def upload(
        self,
        stop_at: Optional[int] = None,
        wait_on_quota: bool = True,
        poll_interval: float = 5.0,
        max_wait: float = 3600.0,
        quota_callback: Optional[Callable[[dict], None]] = None,
    ):
        """Upload the file with progress tracking and quota-aware retry.

        When the server's storage quota is full (HTTP 507), the uploader
        will poll and wait for space to be freed, then automatically resume
        from where it left off (断点续传).

        Args:
            stop_at: Optional byte offset to stop at
            wait_on_quota: If True, wait and retry when quota is exceeded
            poll_interval: Seconds between polling when waiting for quota
            max_wait: Maximum seconds to wait for quota before giving up
            quota_callback: Called with storage info dict when waiting on quota
        """
        import time

        from tusclient.exceptions import TusCommunicationError, TusUploadFailed

        total_waited = 0.0

        # Create upload if not already created
        if not self.url:
            # Handle potential 507 during creation too
            while True:
                try:
                    self.set_url(self.create_url())
                    self.offset = 0
                    break
                except TusCommunicationError as e:
                    if "507" in str(e) and wait_on_quota:
                        storage_info = self._get_storage_info()
                        if quota_callback:
                            quota_callback(storage_info)
                        time.sleep(poll_interval)
                        total_waited += poll_interval
                        if total_waited >= max_wait:
                            raise RuntimeError(
                                f"Storage quota exceeded during create. " f"Waited {total_waited:.0f}s."
                            ) from e
                    else:
                        raise
        else:
            # Get current offset from server for resume
            self.get_offset()

        self._uploaded_bytes = self.offset

        # Upload chunks
        while self.offset < self.file_size:
            if stop_at and self.offset >= stop_at:
                break

            try:
                self.upload_chunk()
                total_waited = 0.0  # Reset wait counter on success
            except (TusCommunicationError, TusUploadFailed) as e:
                # Check if it's a 507 Storage Quota Exceeded
                if "507" in str(e) and wait_on_quota:
                    if total_waited >= max_wait:
                        raise RuntimeError(f"Storage quota exceeded. Waited {total_waited:.0f}s, giving up.") from e

                    # Query storage status
                    storage_info = self._get_storage_info()
                    if quota_callback:
                        quota_callback(storage_info)

                    # Wait and retry
                    retry_after = poll_interval
                    time.sleep(retry_after)
                    total_waited += retry_after

                    # Re-sync offset from server before retrying
                    try:
                        self.get_offset()
                        self._uploaded_bytes = self.offset
                    except Exception:
                        pass  # Keep current offset if HEAD fails
                else:
                    raise

        return self.url

    def _get_storage_info(self) -> dict:
        """Query server storage status."""
        try:
            import httpx

            headers = {}
            # Get auth headers from the TUS client
            if hasattr(self, "client") and self.client and hasattr(self.client, "headers"):
                headers = dict(self.client.headers or {})
            base_url = self.url.rsplit("/tus/", 1)[0] if self.url else ""
            if not base_url and hasattr(self, "client") and self.client:
                base_url = getattr(self.client, "server_url", "")
            if base_url:
                with httpx.Client(timeout=10.0) as c:
                    r = c.get(f"{base_url}/api/storage", headers=headers)
                    if r.status_code == 200:
                        return r.json()
        except Exception:
            pass
        return {}
