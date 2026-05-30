import asyncio
from dataclasses import dataclass, field


@dataclass
class _DownloadJob:
    url: str
    quality: str
    task: asyncio.Task | None = None
    latest_progress: dict | None = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    final_message: dict | None = None


class DownloadManager:
    """Central coordinator for yt-dlp downloads.

    Usage (inside the WebSocket handler):

        queue = await download_manager.subscribe(url, quality, run_download_fn)
        while True:
            msg = await queue.get()
            await websocket.send_json(msg)
            if msg["status"] in ("completed", "error"):
                break
    """

    def __init__(self) -> None:
        self._jobs: dict[tuple[str, str], _DownloadJob] = {}
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        url: str,
        quality: str,
        run_download_fn,
    ) -> asyncio.Queue:
        """Return a queue that will receive progress/completion messages.

        If a download for (url, quality) is already running the caller is
        attached to that job (no duplicate yt-dlp process is started).
        If not, a new job is started and `run_download_fn` is called.

        `run_download_fn` must be a *synchronous* callable that accepts a
        single keyword argument `progress_hooks: list[callable]` and
        returns a MediaDownloadResponse (exactly like real_download_process).
        """
        key = (url, quality)
        queue: asyncio.Queue = asyncio.Queue()

        async with self._lock:
            job = self._jobs.get(key)

            if job is not None and job.task is not None and not job.task.done():
                if job.latest_progress is not None:
                    await queue.put(job.latest_progress)
                job.subscribers.append(queue)

            elif job is not None and job.final_message is not None:
                await queue.put(job.final_message)

            else:
                job = _DownloadJob(url=url, quality=quality, subscribers=[queue])
                self._jobs[key] = job
                job.task = asyncio.ensure_future(
                    self._run(key, job, run_download_fn)
                )

        return queue

    async def _run(
        self,
        key: tuple[str, str],
        job: _DownloadJob,
        run_download_fn,
    ) -> None:
        loop = asyncio.get_running_loop()

        def _post(message: dict) -> None:
            """Thread-safe: schedule a put on every subscriber queue."""

            def _enqueue():
                for q in job.subscribers:
                    q.put_nowait(message)

            loop.call_soon_threadsafe(_enqueue)

        def progress_hook(d: dict) -> None:
            """Drop-in replacement for the original progress_hook.

            Called from the yt-dlp thread; must not touch the event loop
            directly – use _post() instead.
            """
            if d["status"] == "downloading":
                try:
                    progress = (
                        d.get("downloaded_bytes", 0)
                        / d.get("total_bytes", 1)
                        * 100
                    )
                except Exception:
                    return

                speed = d.get("speed") or 0
                eta = d.get("eta") or 0

                if not speed:
                    return

                message = {
                    "status": "downloading",
                    "detail": {
                        "progress": f"{progress:.1f}%",
                        "speed": f"{speed / 1024 / 1024:.1f} MB/s",
                        "eta": f"{eta // 60}:{eta % 60:02d}",
                        "ext": d.get("filename", "").split(".")[-1],
                    },
                }
                job.latest_progress = message
                _post(message)

            elif d["status"] == "finished":
                filename = d.get("filename", "").split("/")[-1]
                message = {
                    "status": "finished",
                    "detail": {"filename": filename},
                }
                _post(message)

        try:
            download_report = await loop.run_in_executor(
                None,
                lambda: run_download_fn(progress_hooks=[progress_hook]),
            )
            final = {
                "status": "completed",
                "detail": download_report.model_dump(),
            }

        except Exception as e:
            final = {"status": "error", "detail": str(e)}

        job.final_message = final
        _post(final)
        
        async with asyncio.Lock():
            self._jobs.pop(key, None)


download_manager = DownloadManager()
