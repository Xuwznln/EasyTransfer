"""Chunked file downloader with HTTP Range support."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import httpx

from etransfer.client.cache import LocalCache
from etransfer.common.constants import AUTH_HEADER, DEFAULT_CHUNK_SIZE
from etransfer.common.models import DownloadInfo


class ChunkDownloader:
    """Download files in chunks with resume support.

    Uses HTTP Range requests to download files in parallel chunks.
    Supports resume through local chunk cache.
    """

    def __init__(
        self,
        server_url: str,
        token: Optional[str] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_concurrent: int = 5,
        cache: Optional[LocalCache] = None,
        timeout: float = 30.0,
    ) -> None:
        """Initialize downloader.

        Args:
            server_url: Server base URL
            token: API authentication token
            chunk_size: Download chunk size
            max_concurrent: Max concurrent chunk downloads
            cache: Local cache instance
            timeout: Request timeout
        """
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.chunk_size = chunk_size
        self.max_concurrent = max_concurrent
        self.cache = cache or LocalCache()
        self.timeout = timeout

        self._headers = {}
        if token:
            self._headers[AUTH_HEADER] = token

    def _get_download_url(self, file_id: str) -> str:
        """Get download URL for a file."""
        return f"{self.server_url}/api/files/{file_id}/download"

    def get_file_info(self, file_id: str) -> DownloadInfo:
        """Get file information for download.

        Args:
            file_id: File identifier

        Returns:
            DownloadInfo object
        """
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.server_url}/api/files/{file_id}/info/download",
                headers=self._headers,
            )
            response.raise_for_status()
            return DownloadInfo(**response.json())

    def download_chunk(
        self,
        file_id: str,
        chunk_index: int,
        chunk_size: Optional[int] = None,
        total_size: Optional[int] = None,
    ) -> bytes:
        """Download a single chunk.

        Args:
            file_id: File identifier
            chunk_index: Chunk index
            chunk_size: Chunk size (uses default if not specified)
            total_size: Total file size (for last chunk calculation)

        Returns:
            Chunk data
        """
        chunk_size = chunk_size or self.chunk_size

        # Calculate byte range
        start = chunk_index * chunk_size
        end = start + chunk_size - 1

        # Adjust end for last chunk
        if total_size and end >= total_size:
            end = total_size - 1

        headers = self._headers.copy()
        headers["Range"] = f"bytes={start}-{end}"

        url = self._get_download_url(file_id)

        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            return response.content

    def download_file(
        self,
        file_id: str,
        output_path: Path,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        use_cache: bool = True,
    ) -> bool:
        """Download a file with chunked transfer.

        Args:
            file_id: File identifier
            output_path: Output file path
            progress_callback: Progress callback (downloaded_bytes, total_bytes)
            use_cache: Use local chunk cache

        Returns:
            True if download successful
        """
        # Get file info
        info = self.get_file_info(file_id)

        # Calculate chunks
        total_chunks = (info.available_size + self.chunk_size - 1) // self.chunk_size

        # Set up cache metadata
        if use_cache:
            self.cache.set_file_meta(file_id, info.filename, info.available_size, self.chunk_size)

        # Get already cached chunks
        cached_chunks = set()
        if use_cache:
            cached_chunks = set(self.cache.get_cached_chunks(file_id))

        # Download missing chunks
        downloaded_bytes = len(cached_chunks) * self.chunk_size
        if progress_callback and cached_chunks:
            progress_callback(downloaded_bytes, info.available_size)

        with ThreadPoolExecutor(max_workers=self.max_concurrent) as executor:
            futures = {}

            for i in range(total_chunks):
                if i in cached_chunks:
                    continue

                future = executor.submit(
                    self._download_and_cache_chunk,
                    file_id,
                    i,
                    info.available_size,
                    use_cache,
                )
                futures[future] = i

            for future in futures:
                try:
                    chunk_data = future.result()
                    downloaded_bytes += len(chunk_data)

                    if progress_callback:
                        progress_callback(downloaded_bytes, info.available_size)
                except Exception as e:
                    print(f"Error downloading chunk {futures[future]}: {e}")
                    return False

        # Assemble file from cache
        if use_cache:
            success = self.cache.assemble_file(file_id, output_path)
            if success:
                # Clear cache for this file
                self.cache.clear_file(file_id)
            return success

        return True

    def _download_and_cache_chunk(
        self,
        file_id: str,
        chunk_index: int,
        total_size: int,
        use_cache: bool,
    ) -> bytes:
        """Download a chunk and cache it.

        Args:
            file_id: File identifier
            chunk_index: Chunk index
            total_size: Total file size
            use_cache: Whether to cache the chunk

        Returns:
            Chunk data
        """
        data = self.download_chunk(file_id, chunk_index, total_size=total_size)

        if use_cache:
            self.cache.put_chunk(file_id, chunk_index, data)

        return data
