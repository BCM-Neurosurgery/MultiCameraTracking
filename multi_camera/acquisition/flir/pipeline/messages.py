from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np


@dataclass(frozen=True)
class FramePacket:
    base_filename: str
    serial: str
    timestamp_ns: int
    real_time: str
    image: np.ndarray


@dataclass(frozen=True)
class MetadataPacket:
    base_filename: str
    local_time: datetime
    real_time: str
    timestamps: list[int]
    frame_id: list[int]
    frame_id_abs: list[int]
    chunk_serial_data: list[int]
    serial_msg: list[Any]
    camera_serials: list[str]
    exposure_times: list[float]
    frame_rates_requested: list[float]
    frame_rates_binning: list[float]

    @classmethod
    def from_frame_dict(cls, frame: dict, fallback_base_filename: str | None = None) -> "MetadataPacket":
        return cls(
            base_filename=frame.get("base_filename") or fallback_base_filename or "",
            local_time=frame["local_times"],
            real_time=frame["real_times"],
            timestamps=frame["timestamps"],
            frame_id=frame["frame_id"],
            frame_id_abs=frame["frame_id_abs"],
            chunk_serial_data=frame["chunk_serial_data"],
            serial_msg=frame["serial_msg"],
            camera_serials=frame["camera_serials"],
            exposure_times=frame["exposure_times"],
            frame_rates_requested=frame["frame_rates_requested"],
            frame_rates_binning=frame["frame_rates_binning"],
        )

    def to_journal_record(self) -> dict:
        return {
            "real_times": self.real_time,
            "timestamps": self.timestamps,
            "frame_id": self.frame_id,
            "frame_id_abs": self.frame_id_abs,
            "chunk_serial_data": self.chunk_serial_data,
            "serial_msg": self.serial_msg,
            "camera_serials": self.camera_serials,
            "exposure_times": self.exposure_times,
            "frame_rates_requested": self.frame_rates_requested,
            "frame_rates_binning": self.frame_rates_binning,
        }


@dataclass(frozen=True)
class SegmentRecord:
    filename: str
    timestamp_spread: float
    recording_timestamp: datetime

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "timestamp_spread": self.timestamp_spread,
            "recording_timestamp": self.recording_timestamp,
        }
