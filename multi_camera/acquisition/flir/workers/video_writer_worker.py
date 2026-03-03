"""Video writer worker that drains per-camera frame queues into MP4 files."""

from __future__ import annotations

from queue import Queue

import cv2
import numpy as np
from tqdm import tqdm


def write_image_queue(
    vid_file: str,
    image_queue: Queue,
    serial,
    pixel_format: str,
    acquisition_fps: float,
    acquisition_type: str,
    video_segment_len: int,
):
    """
    Write images from image_queue to per-camera MP4 segment files.
    """
    timestamps = []
    real_times = []

    out_video = None
    frame_num = 0

    try:
        while True:
            frame = image_queue.get()
            try:
                if frame is None:
                    break

                real_times.append(frame["real_times"])
                im = frame["im"]

                if pixel_format == "BayerRG8":
                    im = cv2.cvtColor(im, cv2.COLOR_BAYER_RG2RGB)

                if out_video is None and len(real_times) == 1:
                    last_im = im
                elif out_video is None and len(real_times) > 1:
                    vid_file = frame["base_filename"] + f".{serial}.mp4"
                    tqdm.write(f"Writing FPS: {acquisition_fps}")
                    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                    out_video = cv2.VideoWriter(vid_file, fourcc, acquisition_fps, (im.shape[1], im.shape[0]))
                    out_video.write(last_im)
                elif frame_num % video_segment_len == 0 and acquisition_type == "continuous":
                    if out_video is not None:
                        out_video.release()
                    real_times = []

                    vid_file = frame["base_filename"] + f".{serial}.mp4"
                    tqdm.write(f"Writing FPS: {acquisition_fps}")
                    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                    print("writing to", vid_file)
                    out_video = cv2.VideoWriter(vid_file, fourcc, acquisition_fps, (im.shape[1], im.shape[0]))
                    out_video.write(im)
                else:
                    if out_video is not None:
                        out_video.write(im)

                frame_num += 1
            except Exception as exc:
                tqdm.write(f"write_image_queue error ({serial}): {exc}")
            finally:
                image_queue.task_done()
    finally:
        if out_video is not None:
            out_video.release()

    if len(timestamps) > 1:
        ts = np.asarray(timestamps)
        delta = np.mean(np.diff(ts, axis=0)) * 1e-9
        fps = 1.0 / delta
        print(f"Finished writing images. Final fps: {fps}")
    else:
        print("Finished writing images.")
