"""Compatibility for Invisible Playwright's browser-process timeout."""

import logging


logger = logging.getLogger("octobot.browser_runtime")
PAGE_CREATION_TIMEOUT_SECONDS = 120


def new_browser_page(context):
    """Allow a slow Firefox process time to answer its first new-page command.

    Invisible Playwright 0.25.7 hard-codes timeout=30 in op_new_page;
    set_default_timeout() does not reach that protocol call. Keep this adapter
    scoped to this connection and this call, and remove it when upstream offers
    a public setting. The dependency is pinned and real-browser tests exercise
    this private transport path.
    """
    try:
        impl = context._impl_obj
        dispatcher = impl._connection._transport._server.object(impl._guid)
        connection = dispatcher.conn
        original_send = connection.send
    except AttributeError:
        raise RuntimeError(
            "Cannot configure browser page-creation timeout: incompatible "
            "Invisible Playwright transport; rebuild with the pinned dependency"
        ) from None

    def send(method, params=None, session=None, timeout=30.0):
        if method == "Browser.newPage":
            timeout = max(timeout, PAGE_CREATION_TIMEOUT_SECONDS)
        return original_send(method, params, session=session, timeout=timeout)

    logger.debug("Browser page-creation protocol timeout: %s seconds.",
                 PAGE_CREATION_TIMEOUT_SECONDS)
    # Change only this browser connection, never the library's class/global
    # defaults. Other commands retain their original deadlines and no command
    # is retried (a late reply could otherwise create a second tab).
    connection.send = send
    try:
        return context.new_page()
    finally:
        connection.send = original_send
