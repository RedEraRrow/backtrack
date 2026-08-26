"""Shared global state: the navigation breadcrumb and the quit-to-terminal signal.

`q` quits the application from anywhere by raising :class:`QuitToTerminal` on the
spot. There is deliberately no "leave this widget and quit later" flag: `q` is
never a way out of a widget — Esc (and ←/b where the widget has no other use for
them) is what backs out.
"""
NAV_STACK = ["Home"]


class QuitToTerminal(BaseException):
    """Raised to unwind the entire menu stack and exit straight to the terminal.

    Derives from BaseException (not Exception) so it bypasses the broad
    ``except Exception`` handlers in the editors and propagates cleanly up to
    main(), where the alt-screen is restored in a finally block.
    """
