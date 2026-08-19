from deepsee_server.runtime_control import RestartController


def test_restart_requires_explicit_enablement_and_launchd_supervision():
    unsupported = RestartController(enabled=True, environment={})
    disabled = RestartController(
        enabled=False,
        environment={"XPC_SERVICE_NAME": "com.deepsee.gateway"},
    )
    scheduled = []
    terminated = []
    supported = RestartController(
        enabled=True,
        environment={"XPC_SERVICE_NAME": "com.deepsee.gateway"},
        schedule=lambda delay, callback: scheduled.append((delay, callback)),
        terminate=lambda: terminated.append(True),
    )

    assert unsupported.supported is False
    assert disabled.supported is False
    assert supported.supported is True

    supported.request_restart()
    supported.request_restart()
    assert len(scheduled) == 1
    scheduled[0][1]()
    assert terminated == [True]
