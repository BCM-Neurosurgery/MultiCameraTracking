import React from 'react';
import { useState, useContext, useRef } from "react";
import { Row, Image } from "react-bootstrap";
import Accordion from 'react-bootstrap/Accordion';
import { AcquisitionState, useEffectOnce } from "../AcquisitionApi";


const Video = () => {
    const { videoUrl } = useContext(AcquisitionState);
    const [imageSrc, setImageSrc] = useState("");
    const ws = useRef(null);
    const prevUrlRef = useRef(null);

    useEffectOnce(() => {

        ws.current = new WebSocket(videoUrl);

        ws.current.onmessage = (event) => {
            const blob = new Blob([event.data], { type: "image/jpeg" });
            const url = URL.createObjectURL(blob);
            const prev = prevUrlRef.current;
            prevUrlRef.current = url;
            setImageSrc(url);
            if (prev) {
                URL.revokeObjectURL(prev);
            }
        };

        return () => {
            if (ws.current) {
                ws.current.close();
            }
            if (prevUrlRef.current) {
                URL.revokeObjectURL(prevUrlRef.current);
            }
        };
    }, []);

    return (
        <Accordion defaultActiveKey="0" className="g-4 p-2">
            <Accordion.Item eventKey="0">
                < Accordion.Header > Video Preview</Accordion.Header >
                <Accordion.Body>

                    <Row md={10} className="g-4 p-2">
                        <Image id="video_stream" src={imageSrc} rounded />
                    </Row>
                </Accordion.Body>
            </Accordion.Item >
        </Accordion >
    );
};

export default Video;