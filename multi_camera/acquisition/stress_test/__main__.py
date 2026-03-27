"""Stress test for the FLIR recording pipeline (no cameras required).

Generates synthetic worst-case frames (random noise) and feeds them through
the real queue → encoder → ffmpeg pipeline to validate throughput under load.

Usage:
    # 4 cameras, 30 fps, 30 seconds
    python -m multi_camera.acquisition.stress_test

    # 8 cameras with segment rollover every 900 frames
    python -m multi_camera.acquisition.stress_test -n 8 -d 60 -s 900

    # Force CPU encoding
    python -m multi_camera.acquisition.stress_test --force-cpu
"""

import argparse
import os
import signal
import sys

from multi_camera.acquisition.stress_test._runner import run_stress_test, StressRecorderShim


def main():
    parser = argparse.ArgumentParser(description="Pipeline stress test with synthetic frames")
    parser.add_argument("-n", "--cameras", type=int, default=4, help="Number of simulated cameras (default: 4)")
    parser.add_argument("-r", "--fps", type=float, default=30.0, help="Target frames per second (default: 30)")
    parser.add_argument("-d", "--duration", type=float, default=30.0, help="Test duration in seconds (default: 30)")
    parser.add_argument("--width", type=int, default=1920, help="Frame width (default: 1920)")
    parser.add_argument("--height", type=int, default=1200, help="Frame height (default: 1200)")
    parser.add_argument("-o", "--output", type=str, default="/tmp/stress_test", help="Output directory (default: /tmp/stress_test)")
    parser.add_argument("-q", "--queue-size", type=int, default=150, help="Image queue size per camera (default: 150)")
    parser.add_argument("-s", "--segment-frames", type=int, default=0, help="Frames per segment, 0 = no segmentation (default: 0)")
    parser.add_argument("--force-cpu", action="store_true", help="Force CPU encoding even if NVENC is available")
    args = parser.parse_args()

    if args.force_cpu:
        os.environ["FORCE_CPU_ENCODE"] = "1"

    # Graceful Ctrl+C — sets stop_recording so the capture loop exits cleanly.
    def sigint_handler(sig, frame):
        print("\nStopping stress test (Ctrl+C)...")
        sys.exit(0)

    signal.signal(signal.SIGINT, sigint_handler)

    print(f"Starting stress test: {args.cameras} cameras, {args.fps} FPS, {args.duration}s")
    if args.segment_frames > 0:
        print(f"  Segment rollover every {args.segment_frames} frames")
    print()

    report = run_stress_test(
        num_cameras=args.cameras,
        fps=args.fps,
        duration_s=args.duration,
        width=args.width,
        height=args.height,
        output_dir=args.output,
        queue_size=args.queue_size,
        segment_frames=args.segment_frames,
    )

    report.print_summary()


if __name__ == "__main__":
    main()
