import React from 'react';
import ReactDOM from 'react-dom';
import { useState, useRef, useEffect, useContext } from "react";
import { Row, Col, Form, Button } from "react-bootstrap";
import { AcquisitionState } from "../AcquisitionApi";

const Participant = () => {
    const { participant, newSession, projectId, projectOptions, setProjectId } = useContext(AcquisitionState);

    const participantRef = useRef(null);

    useEffect(() => {
        const particpantField = ReactDOM.findDOMNode(participantRef.current);
        particpantField.value = participant;
    }, [participant]);

    const [validated, setValidated] = useState(false);

    const handleSubmit = (event) => {
        console.log("handleSubmit", event.target.elements.participant.value);
        event.preventDefault();
        const selectedProject = event.target.elements.projectId
            ? event.target.elements.projectId.value
            : projectId;
        newSession(event.target.elements.participant.value, selectedProject);
        setValidated(true)
    };

    return (
        <Form noValidate validated={validated} onSubmit={handleSubmit} className="p-2">
            <Form.Group controlId="projectId" as={Row} className="mb-3">
                <Form.Label column sm={3}>Project:</Form.Label>
                <Col sm={6}>
                    {projectOptions.length > 0 ? (
                        <Form.Control
                            as="select"
                            value={projectId}
                            onChange={(event) => setProjectId(event.target.value)}
                            required
                        >
                            <option value="" disabled>Select project</option>
                            {projectOptions.map((project) => (
                                <option key={project} value={project}>{project}</option>
                            ))}
                            {projectId && !projectOptions.includes(projectId) && (
                                <option value={projectId}>{projectId}</option>
                            )}
                        </Form.Control>
                    ) : (
                        <Form.Control
                            type="text"
                            value={projectId}
                            onChange={(event) => setProjectId(event.target.value)}
                            placeholder="Project identifier"
                            required
                        />
                    )}
                </Col>
            </Form.Group>
            <Form.Group controlId="participant" as={Row} className="mb-3">
                <Form.Label column sm={3}>Participant:</Form.Label>
                <Col sm={6}>
                    <Form.Control
                        required
                        ref={participantRef}
                        type="text"
                        placeholder="Participant identifier"
                        defaultValue=""
                        aria-describedby="participantHelpBlock"
                    />
                </Col>
                <Col sm={3} className="d-flex justify-content-end">
                    <Button variant="primary" type="submit">
                        New Session
                    </Button>
                </Col>
            </Form.Group>
            {/* <Form.Text id="participantHelpBlock" muted>
                Participant identifier should be unique for each participant, and like p###.
            </Form.Text> */}
        </Form>
    );

};

export default Participant;
