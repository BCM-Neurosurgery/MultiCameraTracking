import React from 'react';
import { useContext, useState } from "react";
import { Container, Row, Col, Form, Button, ProgressBar, Alert, Badge } from "react-bootstrap";
import { AcquisitionState } from "../AcquisitionApi";
import Spinner from 'react-bootstrap/Spinner';
import { FaBullseye, FaEye, FaStop, FaVideo } from "react-icons/fa";

const modeDisplay = {
    idle: {
        title: "READY",
        message: "No acquisition is running.",
        alertVariant: "light",
        badgeVariant: "secondary",
        formClass: "bg-white",
        progressLabel: "Acquisition Progress:",
    },
    preview: {
        title: "PREVIEW ONLY - NOT SAVING",
        message: "Camera frames are being displayed, but no trial video is being saved.",
        alertVariant: "info",
        badgeVariant: "info",
        formClass: "border-info bg-info bg-opacity-10",
        progressLabel: "Preview Progress:",
    },
    recording: {
        title: "RECORDING - SAVING VIDEO",
        message: "A trial recording is active and video files are being saved.",
        alertVariant: "danger",
        badgeVariant: "danger",
        formClass: "border-danger bg-danger bg-opacity-10",
        progressLabel: "Recording Progress:",
    },
    calibration: {
        title: "CALIBRATION - SAVING CALIBRATION VIDEO",
        message: "Calibration capture is active and calibration video is being saved.",
        alertVariant: "warning",
        badgeVariant: "warning",
        formClass: "border-warning bg-warning bg-opacity-10",
        progressLabel: "Calibration Progress:",
    },
};

const RecordingControl = () => {
    const {
        newTrial,
        recordingFilename,
        recordingMode,
        recordingProgress,
        recordingSystemStatus,
        calibrationVideo,
        previewVideo,
        stopAcquisition,
    } = useContext(AcquisitionState);

    const [comment, setComment] = useState("");
    const [maxFrames, setMaxFrames] = useState(1000);

    const isIdle = recordingSystemStatus === "Idle";
    const isAcquiring = recordingSystemStatus === "Recording";
    const activeMode = isIdle ? "idle" : recordingMode || "idle";
    const display = modeDisplay[activeMode] || modeDisplay.idle;
    const outputName = activeMode === "preview" ? "Preview only - no file will be saved" : recordingFilename;

    return (
        <div >
            <Form className={`g-4 p-3 border ${display.formClass}`}>

                <Container>
                    <Alert variant={display.alertVariant} className="mb-3 d-flex justify-content-between align-items-center">
                        <div>
                            <div className="fw-bold">{display.title}</div>
                            <div>{display.message}</div>
                        </div>
                        <Badge bg={display.badgeVariant} text={activeMode === "calibration" ? "dark" : undefined}>
                            {recordingSystemStatus || "Unknown"}
                        </Badge>
                    </Alert>

                    <Row className="g-2 align-items-stretch">
                        <Col md="auto">
                            <Button
                                id="new_trial"
                                variant="success"
                                size="lg"
                                className="fw-bold"
                                disabled={!isIdle}
                                onClick={() => newTrial(comment, maxFrames)}
                            >
                                <FaVideo className="me-2" />
                                Start Recording
                            </Button>
                        </Col>
                        <Col md="auto">
                            <Button
                                id="preview"
                                variant="outline-secondary"
                                disabled={!isIdle}
                                onClick={() => previewVideo(maxFrames)}
                            >
                                <FaEye className="me-2" />
                                Preview Only
                            </Button>
                        </Col>
                        <Col md="auto">
                            <Button
                                id="calibration"
                                variant="outline-warning"
                                disabled={!isIdle}
                                onClick={() => calibrationVideo(maxFrames)}
                            >
                                <FaBullseye className="me-2" />
                                Calibration
                            </Button>
                        </Col>
                        <Col md="auto">
                            <Button
                                id="stop"
                                variant="danger"
                                disabled={!isAcquiring}
                                onClick={() => stopAcquisition()}
                            >
                                <FaStop className="me-2" />
                                Stop
                            </Button>
                        </Col>
                        <Col md="auto" className="d-flex align-items-center">
                            {isAcquiring ? <Spinner animation="border" role="status" /> : null}
                        </Col>
                    </Row>
                </Container>

                <Form.Group as={Row} controlId="max_frames" className="p-2">
                    <Form.Label column sm={3}>Max Frames:</Form.Label>
                    <Col sm={6}>
                        <Form.Control type="number" value={maxFrames} onChange={(e) => setMaxFrames(e.target.value)} />
                    </Col>
                </Form.Group>

                <Form.Group as={Row} controlId="comment" className="p-2">
                    <Form.Label column sm={3}>Comment:</Form.Label>
                    <Col sm={6}>
                        <Form.Control type="text" placeholder="Comment" onChange={(e) => setComment(e.target.value)} />
                    </Col>
                </Form.Group>

                <Form.Group as={Row} controlId="file_name" className="p-2">
                    <Form.Label column sm={3}>Output:</Form.Label>
                    <Col sm={6}>
                        <Form.Control type="text" value={outputName} readOnly />
                    </Col>
                </Form.Group>

                <Row >
                    <Col sm={3}>
                        Acquisition Status:
                    </Col>
                    <Col sm={6} className="text-start">
                        {' '} {recordingSystemStatus}
                    </Col>
                </Row>


                <Row className="p-2">
                    <Col sm={3}>
                        <Form.Label >{display.progressLabel}</Form.Label>
                    </Col>
                    <Col>
                        {isAcquiring ?
                            <ProgressBar now={recordingProgress} label={`${recordingProgress}%`} />
                            : null
                        }
                    </Col>
                </Row>
            </Form>
        </div >
    );

};

export default RecordingControl;
