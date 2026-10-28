from test_admin_frontend_behavior import browser_environment, run_browser


def test_password_browser_behavior():
    run_browser('password_behavior.cjs', browser_environment())
