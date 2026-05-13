import logging


def test_priority_name_to_python_level_mapping():
    from multi_camera.acquisition.flir.spinnaker_logging import _NAME_TO_LEVEL

    assert _NAME_TO_LEVEL["FATAL"] == logging.CRITICAL
    assert _NAME_TO_LEVEL["ERROR"] == logging.ERROR
    assert _NAME_TO_LEVEL["WARN"] == logging.WARNING
    assert _NAME_TO_LEVEL["WARNING"] == logging.WARNING
    assert _NAME_TO_LEVEL["INFO"] == logging.INFO
    assert _NAME_TO_LEVEL["NOTICE"] == logging.INFO
    assert _NAME_TO_LEVEL["DEBUG"] == logging.DEBUG
    assert _NAME_TO_LEVEL["TRACE"] == logging.DEBUG


def test_attach_returns_none_when_system_is_none():
    from multi_camera.acquisition.flir.spinnaker_logging import attach, detach

    handler = attach(None, level="WARN")
    assert handler is None
    detach(None, handler)


def test_attach_returns_none_when_pyspin_unavailable(monkeypatch):
    import multi_camera.acquisition.flir.spinnaker_logging as mod

    monkeypatch.setattr(mod, "_HAS_PYSPIN", False, raising=False)

    class FakeSystem:
        def RegisterLoggingEventHandler(self, *_a, **_kw):
            raise AssertionError("should not be called when PySpin unavailable")

    assert mod.attach(FakeSystem(), level="DEBUG") is None
