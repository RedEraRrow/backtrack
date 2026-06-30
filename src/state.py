"""Shared global state: the navigation breadcrumb and the quit-to-terminal signal."""
NAV_STACK = ["Home"]

# Set by value editors when the user presses `q` (save-current-state-and-quit):
# the editor commits its value as normal, then the next navigation menu sees
# this flag and unwinds to the terminal — so the in-progress edit is saved first.
QUIT_REQUESTED = False


class QuitToTerminal(BaseException):
    """Raised to unwind the entire menu stack and exit straight to the terminal.

    Derives from BaseException (not Exception) so it bypasses the broad
    ``except Exception`` handlers in the editors and propagates cleanly up to
    main(), where the alt-screen is restored in a finally block.
    """
