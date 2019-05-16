# Copyright 2019 Streamlit Inc. All rights reserved.
# -*- coding: utf-8 -*-

import ast
import signal
import sys
import threading
import time

from blinker import Signal

from streamlit import config

try:
    from streamlit.proxy.FileEventObserver import FileEventObserver as FileObserver
except ImportError:
    from streamlit.proxy.PollingFileObserver import PollingFileObserver as FileObserver

from streamlit.logger import get_logger
LOGGER = get_logger(__name__)


class State(object):
    INITIAL = 'INITIAL'
    RUNNING = 'RUNNING'
    STOP_REQUESTED = 'STOP_REQUESTED'
    RERUN_REQUESTED = 'RERUN_REQUESTED'
    PAUSE_REQUESTED = 'PAUSE_REQUESTED'
    PAUSED = 'PAUSED'
    STOPPED = 'STOPPED'


class ScriptRunner(object):

    def __init__(self, report):
        """Initialize.

        Parameters
        ----------
        report : Report
            The report with the script to run.

        """
        self._report = report
        self._state = None

        self._main_thread_id = threading.current_thread().ident
        self._paused = threading.Event()
        self._state_change_requested = threading.Event()

        self.run_on_save = config.get_option('proxy.runOnSave')

        self.on_state_changed = Signal(
            doc="""Emitted when the script's execution state state changes.

            Parameters
            ----------
            state : State
            """)

        self.on_file_change_not_handled = Signal(
            doc="Emitted when the file is modified and we haven't handled it.")

        self._set_state(State.INITIAL)

        self._file_observer = FileObserver(
            self._report.script_path, self.maybe_handle_file_changed)

    def _set_state(self, new_state):
        if self._state == new_state:
            return

        LOGGER.debug('ScriptRunner state: %s -> %s' % (self._state, new_state))
        self._state = new_state
        self.on_state_changed.send(self._state)

    def spawn_script_thread(self):
        LOGGER.debug('Spawning script thread...')

        script_thread = threading.Thread(
            target=self._run,
            name='Streamlit script runner thread')

        script_thread.start()

    def is_running(self):
        return self._state == State.RUNNING

    def is_fully_stopped(self):
        return self._state in (State.INITIAL, State.STOPPED)

    def request_rerun(self, argv):
        self._report.argv = argv

        if self.is_fully_stopped():
            self.spawn_script_thread()
        else:
            self._set_state(State.RERUN_REQUESTED)
            self._paused.clear()
            self._state_change_requested.set()

    def request_stop(self):
        if self.is_fully_stopped():
            pass
        else:
            self._set_state(State.STOP_REQUESTED)
            self._paused.clear()
            self._state_change_requested.set()

    def request_pause_unpause(self):
        if self._state == State.PAUSED:
            self._set_state(State.RUNNING)
            self._paused.clear()
        else:
            self._request_pause()

    def _install_tracer(self):
        """Install function that runs before each line of the script."""

        def trace_calls(frame, event, arg):
            self.maybe_handle_execution_control_request()
            return trace_calls

        # Python interpreters are not required to implement sys.settrace.
        if hasattr(sys, 'settrace'):
            sys.settrace(trace_calls)

    def _run(self):
        # This method should only be called from the script thread.
        _script_thread_id = threading.current_thread().ident
        assert _script_thread_id != self._main_thread_id

        if not self.is_fully_stopped():
            # This should never happen!
            raise RuntimeError('Script is already running')

        # Reset delta generator so it starts from index 0.
        import streamlit as st
        st._reset()

        self._state_change_requested.clear()
        self._set_state(State.RUNNING)

        # Python 3 got rid of the native execfile() command, so we now read the
        # file, compile it, and exec() it. This implementation is compatible
        # with both 2 and 3.
        with open(self._report.script_path) as f:
            filebody = f.read()

        if config.get_option('runner.autoWrite'):
            filebody = _modify_ast(filebody, is_root=True)

        if config.get_option('runner.installTracer'):
            self._install_tracer()

        rerun = False

        try:
            # Compiling must happen in the "try" block, so we can catch things
            # like SyntaxErrors.
            code = compile(
                filebody,
                # Pass in the file path so it can show up in exceptions.
                self._report.script_path,
                # We're compiling entire blocks of Python, so we need "exec"
                # mode (as opposed to "eval" or "single").
                'exec',
                # Don't inherit any flags or "future" statements.
                flags=0,
                dont_inherit=1,
                # Parameter not supported in Python2:
                #optimize=-1,
            )

            # IMPORTANT: must pass a brand new dict into the globals and locals,
            # below, so we don't leak any variables in between runs, and don't
            # leak any variables from this file either.
            # Also: here we set our globals and locals to the same dict to
            # emulate what it's like to run at the top level of a module/python
            # file. This is also why we're adding a few common variables below
            # like __name__.
            namespace = dict(
                __name__='__main__',
                # Convert from unicode for py2.
                __file__=str(self._report.script_path),
            )

            sys.argv = self._report.argv
            exec(code, namespace, namespace)

        except RerunException:
            rerun = True

        except StopException:
            pass

        except BaseException as e:
            # Show exceptions in the Streamlit report.
            st.exception(e)  # This is OK because we're in the script thread.
            # TODO: Clean up the stack trace, so it doesn't include
            # ScriptRunner.

        finally:
            self._set_state(State.STOPPED)

        if rerun:
            self._run()

    def _pause(self):
        self._paused.set()
        self._set_state(State.PAUSED)

        while self._paused.is_set():
            time.sleep(0.1)

    def _request_pause(self):
        if self.is_fully_stopped():
            pass
        else:
            self._set_state(State.PAUSE_REQUESTED)
            self._state_change_requested.set()

    # This method gets called from inside the script's execution context.
    def maybe_handle_execution_control_request(self):
        if self._state_change_requested.is_set():
            LOGGER.debug('Received execution control request: %s', self._state)

            if self._state == State.STOP_REQUESTED:
                raise StopException()
            elif self._state == State.RERUN_REQUESTED:
                raise RerunException()
            elif self._state == State.PAUSE_REQUESTED:
                self._pause()
                return

    def maybe_handle_file_changed(self):
        if self.run_on_save:
            self.request_rerun(self._report.argv)
        else:
            self.on_file_change_not_handled.send()


class ScriptControlException(BaseException):
    """Base exception for ScriptRunner."""
    pass


class StopException(ScriptControlException):
    """Silently stop the execution of the user's script."""
    pass


class RerunException(ScriptControlException):
    """Silently stop and rerun the user's script."""
    pass


def _modify_ast(tree_or_code, is_root):
    """Modify AST so you can use Streamlit without Streamlit calls."""

    if type(tree_or_code) is str:
        tree = ast.parse(tree_or_code)
    else:
        tree = tree_or_code

    for i, node in enumerate(tree.body):
        st_write = None

        # Parse the contents of functions
        if type(node) is ast.FunctionDef:
            node = _modify_ast(node, is_root=False)

        # Convert expression nodes to st.write
        if type(node) is ast.Expr:
            st_write = _get_st_write_from_expr(node, i)

        # Convert assignments to st.write
        elif type(node) is ast.Assign:
            st_write = _get_st_write_from_assign(node, i)

        if st_write is not None:
            node.value = st_write

    if is_root:
        # Import Streamlit so we can use it in the st_write's above.
        _insert_import_statement(tree)

    ast.fix_missing_locations(tree)

    return tree


def _insert_import_statement(tree):
    """Insert Streamlit import statement at the top(ish) of the tree."""

    st_import = _build_st_import_statement()

    # If the 0th node is already an import statement, put the Streamlit
    # import below that, so we don't break "from __future__ import".
    if tree.body and type(tree.body[0]) in (ast.ImportFrom, ast.Import):
        tree.body.insert(1, st_import)

    # If the 0th node is a docstring and the 1st is an import statement,
    # put the Streamlit import below those, so we don't break "from
    # __future__ import".
    elif (
        len(tree.body) > 1
        and (
            type(tree.body[0]) is ast.Expr and
            type(tree.body[0].value) is ast.Str
        )
        and type(tree.body[1]) in (ast.ImportFrom, ast.Import)):
        tree.body.insert(2, st_import)

    else:
        tree.body.insert(0, st_import)


def _build_st_import_statement():
    """Build AST node for `import streamlit as __streamlit__`."""
    return ast.Import(
        names = [ast.alias(
            name='streamlit',
            asname='__streamlit__',
        )],
    )


def _build_st_write_call(nodes):
    """Build AST node for `__streamlit__._transparent_write(*nodes)`."""
    return ast.Call(
        func=ast.Attribute(
            attr='_transparent_write',
            value=ast.Name(id='__streamlit__', ctx=ast.Load()),
            ctx=ast.Load(),
        ),
        args=nodes,
        keywords=[],
        kwargs=None,
        starargs=None,
    )


def _get_st_write_from_expr(node, i):
    # Don't change function calls
    if type(node.value) is ast.Call:
        return None

    # Don't change Docstring nodes
    if type(node.value) is ast.Str:
        if i == 0:
            return None

    # If 1-element tuple, call st.write on the 0th element (rather than the
    # whole tuple). This allows us to add a comma at the end of a statement
    # to turn it into an expression that should be st-written. Ex:
    # "np.random.randn(1000, 2),"
    if (type(node.value) is ast.Tuple and
            len(node.value.elts) == 1):
        args = node.value.elts
        st_write = _build_st_write_call(args)

    # st.write all strings.
    elif type(node.value) is ast.Str:
        args = [node.value]
        st_write = _build_st_write_call(args)

    # st.write all variables, and also print the variable's name.
    elif type(node.value) is ast.Name:
        args = [
            ast.Str(s='**%s**' % node.value.id),
            node.value
        ]
        st_write = _build_st_write_call(args)

    # st.write everything else
    else:
        args = [node.value]
        st_write = _build_st_write_call(args)

    return st_write


def _get_st_write_from_assign(node, i):
    """Replace "foo = bar()," with "foo = st._transparent_write(bar())"."""
    # Only convert if assigning to a 1-element tuple

    if type(node.value) is not ast.Tuple:
        return None

    if len(node.value.elts) != 1:
        return None

    elt = node.value.elts[0]
    st_write = _build_st_write_call([elt])

    return st_write
