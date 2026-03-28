import os
from pathlib import Path

import numpy as np
import datajoint as dj


def _load_datajoint_config():
    """Load project-local DataJoint config if present."""
    root = Path(__file__).resolve().parents[2]
    for filename in ("datajoint_config.json", "datajoint.json", "dj_local_conf.json"):
        cfg_path = root / filename
        if cfg_path.exists():
            dj.config.load(str(cfg_path))
            return


_load_datajoint_config()
schema = dj.schema("multicamera_tracking")


# keeping this class definition in this file to avoid it needing to depend
# on the pose pipeline, which is required for the rest of the class definitions


@schema
class Calibration(dj.Manual):
    definition = """
    # Calibration of multiple camera system
    cal_timestamp        : timestamp
    camera_config_hash   : varchar(10)
    ---
    recording_base       : varchar(50)
    num_cameras          : int
    camera_names         : longblob   # list of camera names
    camera_calibration   : longblob   # calibration results
    reprojection_error   : float
    calibration_points   : longblob
    calibration_shape    : longblob
    calibration_type=""  : varchar(50)
    """


def run_calibration(vid_base, vid_path=None, checkerboard_size=109.0, checkerboard_dim=(5, 7), charuco=True):
    from ..analysis.calibration import run_calibration

    if vid_path is None:
        import os

        vid_path, vid_base = os.path.split(vid_base)

    print(vid_path, vid_base)

    entry = run_calibration(
        vid_base,
        vid_path,
        checkerboard_size=checkerboard_size,
        checkerboard_dim=checkerboard_dim,
        charuco=charuco,
        jax_cal=False,
    )

    if np.isnan(entry["reprojection_error"]):
        raise Exception(f"Calibration failed: {entry}")

    if entry["reprojection_error"] > 0.3:
        print(entry)
        print(f'The error was {entry["reprojection_error"]}. Are you sure you would like to store this in the database? [Yes/No]')

        response = input()
        if response[0].upper() != "Y":
            print("Cancelling")
            return

    entry["recording_base"] = vid_base

    if charuco:
        entry["calibration_type"] = "charuco"

    Calibration.insert1(entry)

    # Export calibration to portable .npz file alongside the videos
    if vid_path:
        npz_path = os.path.join(vid_path, vid_base + ".calibration.npz")
        np.savez(
            npz_path,
            camera_names=entry["camera_names"],
            mtx=entry["camera_calibration"]["mtx"],
            dist=entry["camera_calibration"]["dist"],
            rvec=entry["camera_calibration"]["rvec"],
            tvec=entry["camera_calibration"]["tvec"],
            reprojection_error=entry["reprojection_error"],
            calibration_points=entry["calibration_points"],
            calibration_shape=entry["calibration_shape"],
        )
        print(f"Calibration exported to {npz_path}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compute calibration from specified videos and insert into database")
    parser.add_argument("vid_base", help="Base filenames to use for calibration")
    parser.add_argument("--vid_path", help="Path to files", default=None)
    # checkerboard_size=110.0, checkerboard_dim=(4, 6)
    parser.add_argument(
        "--checkerboard_size",
        help="Size of checkerboard squares",
        default=110.0,
        type=float,
    )
    parser.add_argument(
        "--checkerboard_dim",
        help="Number of squares in checkerboard (rows, columns)",
        default=(4, 6),
        type=lambda x: tuple(map(int, x.split(","))),
    )
    parser.add_argument(
        "--charuco",
        help="using a charuco board instead of a checkerboard. Default is False.",
        action="store_true",
    )
    args = parser.parse_args()
    run_calibration(
        vid_base=args.vid_base,
        vid_path=args.vid_path,
        checkerboard_size=args.checkerboard_size,
        checkerboard_dim=args.checkerboard_dim,
        charuco=args.charuco,
    )

    print("Complete")
