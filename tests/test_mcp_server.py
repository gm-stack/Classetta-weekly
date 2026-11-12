"""The MCP server: the protocol on the wire, and the tools behind it.

The SDK's own client against a real socket on localhost, plus raw HTTP for the
things a well-behaved client never sends -- the malformed body, the wrong
Content-Type, the web page that resolved `localhost` to this port.

The emulator behind it is test_debugger's machine: a loop a guest thread runs,
so `pause` answers with a core that really has stopped rather than with a stub's
idea of one.

Most of the protocol is the SDK's business now, so most of what's checked here
is what must not drift: every debugger command and every script power present,
each described, the schemas generated from the signatures rather than written
twice; a tool that can't be done reaching the client in its own words rather
than the SDK's; the emulator's one log staying one.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from macrt.dbg import Commands
from macrt.mcpserver import McpServer, _quiet, _security, _tools

from test_debugger import CODE, LOOP_SIZE, Guest, machine


@pytest.fixture
def server(qapp):
    """An emulator to look at and a server on it, on a port the OS picked, on a
    machine with a guest thread running the loop.  (Port 0: two test runs on one
    machine then can't collide over a fixed one.)

    A namespace rather than a tuple, because most tests want the server only and
    a name reads better than a position when four of them are in play."""
    mem, cpu = machine()
    guest = Guest(cpu)
    display = _Display(guest.debug)
    mcp = McpServer(display, host="127.0.0.1", port=0)
    url = mcp.start()
    assert url.startswith("http://127.0.0.1:") and url.endswith("/mcp")
    try:
        yield SimpleNamespace(mcp=mcp, display=display, guest=guest)
    finally:
        mcp.stop()
        guest.close()


@pytest.fixture
def client(server):
    """The server, wrapped in a connected client.  Each call opens its own
    session in its own event loop: the client is an anyio task group and
    doesn't survive a loop ending, and a tool call is one round trip."""
    return _Client(server.mcp.url, server)


class _System:
    """A machine and nothing else.  No model, so a Controller built on this
    display would fail on input -- which is why the driving tools aren't
    exercised against it: they need a screen and a keyboard."""

    def __init__(self, debug):
        self.machine = None if debug is None else type("M", (), {"debug": debug})()
        self.model = None


class _Display:
    """Enough of a display for the tools: a machine, no processes, and no
    bridge -- this thread isn't the GUI thread, and reading a core needs
    nothing from it."""

    def __init__(self, debug=None):
        self.system = _System(debug)
        self.processes = []
        self.bridge = None
        self.screen_widget = None
        self.front_process = None
        self.dbg_commands = None


class _Client:
    """The SDK's client, used the way a client uses it, with the answer's text
    and structure pulled out the way a caller would."""

    def __init__(self, url, server=None):
        self.url = url
        # So a test can check what happened to the machine behind the reply.
        self.server = server
        self.guest = None if server is None else server.guest

    def call(self, name, /, **arguments):
        """A tool call, as a `_Result`.  `name` positional-only: several tools
        take an argument called `name` themselves."""
        return _run(self._call(name, arguments))

    def discover(self):
        """What the server says it is, and what it offers: (info, tools)."""
        return _run(self._discover())

    async def _call(self, name, arguments):
        async with _session(self.url) as s:
            await s.initialize()
            return _Result(await s.call_tool(name, arguments))

    async def _discover(self):
        async with _session(self.url) as s:
            info = await s.initialize()
            return info, (await s.list_tools()).tools


class _Result:
    """A tools/call answer: the text a person reads, the structure a program
    reads, and whether it failed."""

    def __init__(self, result):
        self.raw = result
        self.is_error = result.is_error
        self.data = result.structured_content
        self.text = "\n".join(c.text for c in result.content
                              if getattr(c, "type", None) == "text")
        self.blocks = [c.type for c in result.content]

    def __repr__(self):
        return f"<Result error={self.is_error} text={self.text[:200]!r}>"


@asynccontextmanager
async def _session(url):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            yield session


def _run(coroutine):
    """An async call, from a synchronous test.  pytest-asyncio isn't installed;
    asyncio.run is enough, and each call getting a loop of its own is a feature
    rather than a cost."""
    return asyncio.run(coroutine)


# ---------------------------------------------------------------------------
# The handshake, and what a client is told about this server
# ---------------------------------------------------------------------------
def test_the_handshake_names_the_server_and_says_the_conventions(client):
    """The first thing a client learns has to carry the two things it can't
    guess: that `ram:` means something here, and that a running program has no
    registers to read."""
    info, tools = client.discover()
    assert info.server_info.name == "classetta"
    assert info.protocol_version                      # whatever the client asked for
    assert "ram:" in info.instructions
    assert "pause" in info.instructions


def test_the_tool_list_is_complete_and_each_one_is_described(client, server):
    """Every debugger command and every script power, each with a schema a
    client can build a form from -- what it takes, which of those it must take,
    and why anyone would want it.

    The schemas are generated from the tool methods' signatures, so this is
    also the check that nothing was added to Tools without being offered, or
    offered with an argument nobody described."""
    server = server.mcp
    _, tools = client.discover()
    by_name = {t.name: t for t in tools}
    assert sorted(by_name) == sorted(name for name, _ in _tools(server.tools))
    wanted = ["status", "pause", "step", "cont", "command",
              "breakpoint", "clear_breakpoint", "break_trap", "clear_break_trap",
              "break_memory", "clear_break_memory", "list_breakpoints",
              "read_memory", "write_memory", "resolve_address", "dump_ram",
              "find_symbol", "call_function", "regs",
              "screenshot", "press", "key_down", "key_up", "type_text",
              "click", "mouse_down", "mouse_up", "mouse_move",
              "processes", "front", "attach_process", "windows", "wait_window",
              "sleep", "trace_functions", "trace_ready", "trace_stop", "quit"]
    assert sorted(by_name) == sorted(wanted), set(wanted) ^ set(by_name)
    for name, tool in by_name.items():
        schema = tool.input_schema
        assert schema["type"] == "object", name
        assert (tool.description or "").strip(), name
        required = set(schema.get("required", []))
        assert required <= set(schema["properties"]), name
        for argument, spec in schema["properties"].items():
            assert spec.get("type"), f"{name}.{argument} says no type"
            assert (spec.get("description") or "").strip(), f"{name}.{argument} says nothing"
    # The one tool that must not be triggerable by an empty call.
    assert "confirm" in by_name["quit"].input_schema["required"]


def test_an_address_argument_says_both_writes_of_it(client):
    """The part of the convention a client can't infer from a schema: that
    `ram:` is accepted at all.  Said on every tool that takes an address,
    because that's where someone would look."""
    _, tools = client.discover()
    by_name = {t.name: t for t in tools}
    for name in ("breakpoint", "clear_breakpoint", "break_memory", "read_memory",
                 "write_memory", "resolve_address"):
        where = by_name[name].input_schema["properties"]["address"]["description"]
        assert "ram:" in where, name


def test_limits_a_tool_advertises_are_limits_it_enforces(client):
    """`lines` says 1..64, so a client with a form can refuse 999 before sending
    it -- and the server refuses it too, rather than answering a dump of a size
    nobody agreed to."""
    _, tools = client.discover()
    schema = next(t for t in tools if t.name == "read_memory").input_schema
    assert schema["properties"]["lines"]["minimum"] == 1
    assert schema["properties"]["lines"]["maximum"] == 64
    result = client.call("read_memory", address=f"${CODE:06X}", lines=999)
    assert result.is_error and "lines" in result.text


def test_a_dict_tool_answer_comes_as_text_and_as_structure(client):
    """A program reads the fields; a person reading the transcript reads the
    text.  Both are there -- which is why the dict-returning tools say
    `dict[str, Any]`: a bare `dict` publishes no output schema, and the caller
    would get prose only.  The SDK writes the text block by serializing the
    same dict, so the two can't disagree."""
    result = client.call("status")
    assert result.data["running"] is True
    assert "running" in result.text
    assert json.loads(result.text) == result.data


def test_a_list_tool_answer_is_its_lines(client):
    """The lines a command answers with are the lines a person would have seen
    at the prompt, in order, one text block joining them."""
    client.call("breakpoint", address=f"${CODE + 4:06X}")
    assert client.call("command", line="bp").text == f"b   ${CODE + 4:06X}"
    client.call("clear_breakpoint", address=f"${CODE + 4:06X}")


# ---------------------------------------------------------------------------
# status: the question asked first
# ---------------------------------------------------------------------------
def test_status_says_the_program_is_running_while_it_runs(client):
    result = client.call("status")
    assert result.data["running"] is True
    assert "running" in result.text
    # No registers while it runs: there's no consistent machine to read.
    assert "pc" not in result.data


def test_pause_stops_it_and_status_says_where(client):
    from test_debugger import consistent
    result = client.call("pause")
    status = result.data
    assert status["paused"] is True, status
    assert status["reason"] == "Pause pressed"
    pc = int(status["pc"], 16)
    assert consistent(pc, int(status["d"][0], 16))       # a whole instruction
    assert len(status["d"]) == 8 and len(status["a"]) == 7
    assert status["sr"].startswith("0x") and len(status["flags"]) == 5
    assert sum(1 for row in status["listing"] if row["at_pc"]) == 1
    assert status["sp"].startswith("0x") and status["mode"]
    assert "app_name" in status and status["symbols"] is False
    # Everything the window draws as a register panel is in the structure: the
    # flags as letters and as bits, so a client needn't decode SR itself.
    assert status["flag_bits"]["supervisor"] is True
    assert status["where"].startswith(f" at ${CODE:06X}")
    assert "no symbol table" in status["where"]      # nothing was loaded to name it
    client.call("cont")


def test_step_runs_one_instruction_more(client):
    from test_debugger import NEXT
    before = int(client.call("pause").data["pc"], 16)
    after = int(client.call("step").data["pc"], 16)
    assert (after - CODE) % LOOP_SIZE == NEXT[(before - CODE) % LOOP_SIZE], \
        (hex(before), hex(after))
    client.call("cont")


def test_stepping_a_running_program_says_it_only_stopped_it(client):
    """The honest answer to a step asked while it runs free: it's stopped now,
    and you didn't get one instruction run on a game you hadn't meant to
    single-step."""
    result = client.call("step")
    assert result.data["paused"]
    assert "step again" in result.data["message"], result
    client.call("cont")


def test_continue_lets_it_go(client):
    client.call("pause")
    result = client.call("cont")
    assert result.data["running"] is True, result
    assert "running" in result.text


def test_regs_reads_the_stopped_machine(client):
    """The registers while the debugger holds the core — the one case where
    reading them matters, and the case the script layer refuses, because its
    `acquire` is the thing the stop already holds.  Against the real game over
    a socket it answered "call from a hook handler or async_call", which is
    no use to anyone who has just paused a game to look."""
    stopped = client.call("pause").data
    regs = client.call("regs")
    assert not regs.is_error, regs.text
    assert int(regs.data["pc"], 16) == int(stopped["pc"], 16)
    assert regs.data["d0"] == stopped["d"][0]
    assert regs.data["sp"] == stopped["sp"]
    assert regs.data["a7"] == regs.data["sp"]
    assert len(regs.data) == 8 + 7 + 2 + 2          # d0-d7, a0-a6, sp and a7, pc, sr
    client.call("cont")


# ---------------------------------------------------------------------------
# Breakpoints, through the tools and through the window's grammar
# ---------------------------------------------------------------------------
def test_the_tools_and_the_command_line_agree(client, server):
    """`command` is the window's grammar, so a breakpoint made through it and
    one made through the breakpoint tool are the same breakpoint."""
    guest = client.guest
    said = client.call("command", line=f"b ${CODE + 4:06X}")
    assert "breakpoint at" in said.text
    assert guest.debug.bp_addr == {CODE + 4}
    assert client.call("list_breakpoints").text == f"b   ${CODE + 4:06X}"
    client.call("command", line=f"cb ${CODE + 4:06X}")
    assert guest.debug.bp_addr == set()
    assert client.call("list_breakpoints").text == ""


def test_the_command_line_tool_answers_what_the_window_would(client):
    help = client.call("command", line="?").text
    assert "commands:" in help and "dm" in help and "sm" in help
    assert "don't know the command" in client.call("command", line="frobnicate").text
    assert "no trap called" in client.call("command", line="bt Nonesuch").text


def test_a_breakpoint_made_by_the_tool_is_listed_and_cleared(client):
    assert "breakpoint at" in client.call("breakpoint", address=f"${CODE:06X}").text
    assert "trap" in client.call("break_trap", name="WaitNextEvent").text
    assert "write of" in client.call("break_memory", address="$2000").text
    armed = client.call("list_breakpoints").text.splitlines()
    assert any(row.startswith("b  ") for row in armed), armed
    assert any(row.startswith("bt ") for row in armed), armed
    assert any(row.startswith("bmw") for row in armed), armed
    client.call("clear_breakpoint", address=f"${CODE:06X}")
    client.call("clear_break_trap", name="WaitNextEvent")
    client.call("clear_break_memory", address="$2000")
    assert client.call("list_breakpoints").text == ""


def test_a_memory_breakpoint_can_be_put_on_a_read(client):
    assert "read of" in client.call("break_memory", address="$2000", mode="read").text
    assert client.call("list_breakpoints").text.startswith("bmr")
    client.call("clear_break_memory", address="$2000", mode="read")


def test_a_mode_that_is_neither_read_nor_write_is_refused(client):
    result = client.call("break_memory", address="$2000", mode="sideways")
    assert result.is_error and "neither read nor write" in result.text


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
def test_read_memory_gives_the_bytes_and_their_text(client, server):
    first = client.call("read_memory", address=f"${CODE:06X}", lines=1).data
    assert first["text"].startswith(f"{CODE:08X}  52 40 54 40 58 40 5F 40")
    assert first["bytes"].startswith("52405440")
    assert first["ascii"].startswith("R@T@")
    assert int(first["next_address"], 16) == CODE + 16
    # The next read goes on from there, which is how you walk memory.
    again = client.call("read_memory", address=first["next_address"], lines=1).data
    assert again["next_address"] == f"0x{CODE + 32:08X}"


def test_write_memory_writes_what_its_digits_say(client, server):
    guest = client.guest
    mem = guest.debug.cpu.mem
    result = client.call("write_memory", address="$2000", value="CAFE")
    assert "word" in result.text and mem.r16(0x2000) == 0xCAFE
    client.call("write_memory", address="$2004", value="9")
    assert mem.r8(0x2004) == 0x09 and mem.r8(0x2005) == 0x00     # one byte only
    client.call("write_memory", address="$2008", value="11223344")
    assert mem.r32(0x2008) == 0x11223344


def test_write_memory_says_what_it_overwrote(client, server):
    guest = client.guest
    mem = guest.debug.cpu.mem
    mem.write(0x2100, b"\xAA\xBB\xCC\xDD")
    text = client.call("write_memory", address="$2100", value="1234").text
    assert "was AA BB" in text, text


def test_resolve_address_answers_with_the_ones_it_has(client):
    """No application to translate against here, so only the machine's own
    number is known -- and the answer says so rather than inventing one."""
    where = client.call("resolve_address", address=f"${CODE:06X}").data
    assert where["live"] == f"0x{CODE:08X}"
    assert where["ghidra"] is None
    assert where["shown"] == f"${CODE:08X}"


# ---------------------------------------------------------------------------
# A tool that can't be done is an answer, not a broken server
# ---------------------------------------------------------------------------
def test_a_ram_address_with_nothing_to_translate_says_what_it_lacks(client):
    """Machine, but no application whose map to use.  The reason has to reach
    the client in its own words: the SDK replaces an exception escaping a tool
    with "Error executing tool X" and keeps the message on the server, which is
    right for a crash and wrong for an answer someone came for."""
    result = client.call("breakpoint", address="ram:10000")
    assert result.is_error is True
    assert "running 68k application" in result.text


def test_a_tool_that_raises_comes_back_as_an_error_result_not_a_crash(client):
    result = client.call("write_memory", address="$FFFFFF00", value="11223344")
    assert result.is_error is True
    assert "outside RAM" in result.text
    # The server is still there to be asked.
    assert client.call("status").data["running"]


def test_quit_needs_to_be_meant(client, server):
    """Not something to do by accident, and not something a client can do by
    guessing the tool's name alone."""
    display = server.display
    result = client.call("quit", confirm="no")
    assert result.is_error and "confirm" in result.text
    assert display.processes == []           # and nothing was shut down


def test_a_quit_landing_mid_shutdown_answers_instead_of_complaining():
    """Shutdown closes the bridge before the things under it go away, so a call
    arriving then comes back with ProcessKilled.  The client asked for the
    shutdown; being told the machine is dying, in the emulator's own exception's
    words, is a bad answer to its own request."""
    from macrt.toolbox.events import ProcessKilled

    class _Dying:
        def quit(self):
            raise ProcessKilled()

    from macrt.mcpserver import Tools
    assert "shutting down" in Tools(_Display(None), _Dying()).quit("yes")


def test_wait_window_with_no_title_waits_for_any_window():
    """An empty title is what a client that left the argument out sends, and it
    means "any window" -- passed to the Controller as a title, it would wait for
    a window actually named "" and time out on a game with one on screen."""
    asked = []

    class _Patient:
        def wait_window(self, timeout, title):
            asked.append((timeout, title))

    from macrt.mcpserver import Tools
    display = _Display(None)
    for title in (None, ""):
        Tools(display, _Patient()).wait_window(title=title, timeout=5)
    assert asked == [(5, None), (5, None)]
    Tools(display, _Patient()).wait_window(title="Score", timeout=5)
    assert asked[-1] == (5, "Score")
    Tools(display, _Patient()).wait_window(title="  ", timeout=5)
    assert asked[-1] == (5, "  ")          # a blank title is a title, not a blank


def test_a_screenshot_takes_the_front_window_when_there_is_no_whole_screen():
    """The default desktop mode gives each Mac window its own host window, so
    there's no single screen widget to grab -- and the front application's front
    window is the thing someone asking for a screenshot means.  With nothing on
    screen at all, the refusal says which of the two is missing."""
    from macrt.mcpserver import _front_view, _grab_png

    class _View:
        def __init__(self, visible=True):
            self.visible = visible

        def isVisible(self):
            return self.visible

    class _Screened(_Display):
        """Front app "app", with `_pairs` as its z-order would yield it."""

        def __init__(self, pairs):
            super().__init__(None)
            self.front_process = "app"
            self.views = {}
            self.pairs = pairs

        def z_order(self):
            return iter(self.pairs)

    one, other, hidden = _View(), _View(), _View(visible=False)
    screened = _Screened([("app", one), ("app", hidden), ("other", other)])
    screened.views = {one: one, other: other, hidden: hidden}
    assert _front_view(screened) is one          # front app's front *visible* one
    screened.pairs = [("other", other)]
    assert _front_view(screened) is None         # another app's window isn't taken
    screened.pairs = [("app", hidden)]
    assert _front_view(screened) is None         # and a hidden one isn't either

    with pytest.raises(RuntimeError) as e:
        _grab_png(_Display(None))                # nothing on screen, no screen widget
    assert "--host screen" in str(e.value)


def test_a_missing_argument_is_refused_before_the_tool_runs(client):
    """Validated against the generated schema: the client is told which argument
    it left out, and the tool never saw a half-filled call."""
    result = client.call("write_memory", address="$100")
    assert result.is_error and "value" in result.text


# ---------------------------------------------------------------------------
# Only this machine
# ---------------------------------------------------------------------------
def test_an_origin_from_elsewhere_is_refused(server):
    """A web page resolving `localhost` to this port -- DNS rebinding, or a page
    on the same machine deciding to drive the emulator -- gets nothing."""
    mcp, display = server.mcp, server.display
    for bad in ("http://evil.example.com", "http://10.0.0.5:8765",
                "https://evil.test", "file://"):
        status, body = _post(mcp.url, _initialize(), headers={"Origin": bad})
        assert status == 403, (bad, status, body)
    for good in ("http://localhost:5173", "http://127.0.0.1:3000", "null",
                 "http://[::1]:8080"):
        status, _ = _post(mcp.url, _initialize(), headers={"Origin": good})
        assert status == 200, (good, status)


def test_a_host_that_isn_t_this_machine_is_refused(server):
    """The other half: a rebinding page's requests carry the name it resolved,
    not 127.0.0.1, and that's what's refused here."""
    mcp = server.mcp
    for bad in ("evil.example.com", "10.0.0.5:1234", "localhost.evil.com"):
        status, _ = _post(mcp.url, _initialize(), headers={"Host": bad})
        assert status == 421, bad
    for good in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765"):
        status, _ = _post(mcp.url, _initialize(), headers={"Host": good})
        assert status == 200, good


def test_which_hosts_and_origins_are_this_machine():
    """The list, in the form the SDK matches it in: exact strings, plus `:*` for
    any port.  What's on the list is the whole policy, so it's worth seeing
    rather than being implied."""
    settings = _security("127.0.0.1")
    assert settings.enable_dns_rebinding_protection
    assert settings.allowed_hosts == ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    assert "http://127.0.0.1:*" in settings.allowed_origins
    assert "http://localhost:*" in settings.allowed_origins
    assert not any("*" in h and not h.endswith(":*") for h in settings.allowed_hosts)


def test_a_request_without_a_json_content_type_is_not_run(server):
    """A form post from a page on this machine isn't a JSON-RPC message, and
    running it would be a way of driving the emulator without the Origin check
    having anything to say about it."""
    mcp = server.mcp
    request = urllib.request.Request(mcp.url, data=json.dumps(_initialize()).encode(),
                                     headers={"Accept": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(request, timeout=8)
    assert e.value.code == 400


def test_malformed_json_is_answered_not_hung_on(server):
    mcp = server.mcp
    status, answer = _post(mcp.url, None, raw=b"{this is not json")
    assert status == 400
    assert answer["error"]["code"] == -32700
    assert _post(mcp.url, _initialize())[0] == 200      # and it's still itself


def test_a_body_that_isnt_json_rpc_is_refused(server):
    """A message with no jsonrpc: 2.0 in it isn't an RPC: it's refused, not
    run."""
    mcp = server.mcp
    status, answer = _post(mcp.url, {"id": 1, "method": "ping"})
    assert status == 400 and answer["error"]["code"] == -32602, answer
    assert _post(mcp.url, _initialize())[0] == 200


def test_an_unknown_method_is_named(server):
    mcp = server.mcp
    _post(mcp.url, _initialize())
    status, answer = _post(mcp.url, {"jsonrpc": "2.0", "id": 9,
                                     "method": "things/happen"})
    assert status == 200
    assert answer["error"]["code"] == -32601
    assert answer["error"]["data"] == "things/happen"


def test_an_unknown_tool_is_an_error_result_the_client_can_read(server):
    """A tool that isn't there is a failed call rather than a failed transport,
    so a client that guessed a name finds out which one and carries on."""
    mcp = server.mcp
    _post(mcp.url, _initialize())
    status, answer = _post(mcp.url, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                     "params": {"name": "frob", "arguments": {}}})
    assert status == 200 and answer["result"]["isError"] is True
    assert "frob" in answer["result"]["content"][0]["text"]


def test_notifications_are_accepted_and_answered_nothing(server):
    """A notification is said, not asked: no id, and nothing comes back but an
    accepted."""
    mcp = server.mcp
    status, body = _post(mcp.url, {"jsonrpc": "2.0",
                                   "method": "notifications/initialized"})
    assert status == 202 and body is None


def test_resources_and_prompts_are_answered_as_having_none(server):
    """The server has neither, and says so in the shape asked for rather than
    with a method-not-found some clients read as a broken server."""
    mcp = server.mcp
    _post(mcp.url, _initialize())
    assert _post(mcp.url, {"jsonrpc": "2.0", "id": 1,
                           "method": "resources/list"})[1]["result"] == {"resources": []}
    assert _post(mcp.url, {"jsonrpc": "2.0", "id": 2,
                           "method": "prompts/list"})[1]["result"] == {"prompts": []}


def test_the_url_is_the_one_the_config_says(server):
    """`/mcp` is what the log tells someone to put in the client's config, and
    it's where the server answers.  Anything else is a plain 404: this is one
    endpoint, not a site, and `/` isn't a courtesy redirect to it."""
    mcp = server.mcp
    base = mcp.url[: -len("/mcp")]
    assert _post(mcp.url, _initialize())[0] == 200
    assert _post(base + "/elsewhere", _initialize())[0] == 404
    assert _post(base, _initialize())[0] == 404


def test_a_get_is_an_open_stream_and_not_a_way_to_read_state(server):
    """There's nothing to stream -- the emulator's state is read when asked
    about -- but the transport holds a GET open the way the protocol allows
    rather than answering 405 and making a client that probes with one give up.
    Nothing is read or changed by it, which is what's checked."""
    mcp, display = server.mcp, server.display
    status, _ = _post(mcp.url, None, method="GET",
                      headers={"Accept": "text/event-stream"}, timeout=2)
    assert status == "open", status            # opened and stayed open
    assert display.processes == []             # and nothing happened
    assert _post(mcp.url, _initialize())[0] == 200      # and the server still answers


# ---------------------------------------------------------------------------
# The server's place in the emulator: one log, one set of commands
# ---------------------------------------------------------------------------
def test_the_emulator_s_log_is_not_taken_over_and_prints_everything_twice(qapp):
    """MCPServer's constructor calls logging.basicConfig for the whole process,
    which would put a second handler on the root alongside the one macrt set up
    -- and every line the emulator logged would appear twice, in a format nobody
    chose.  What the SDK adds goes under its own name instead."""
    from macrt import ansi
    root, sdk = logging.getLogger(), logging.getLogger("mcp")
    saved = [(name, list(logging.getLogger(name).handlers),
              logging.getLogger(name).level, logging.getLogger(name).propagate)
             for name in ("", "macrt", "mcp")]
    try:
        ansi.setup(None, logging.INFO)
        mine, level = list(root.handlers), root.level
        mem, cpu = machine()
        guest = Guest(cpu)
        server = McpServer(_Display(guest.debug), host="127.0.0.1", port=0)
        server.start()
        server.stop()
        guest.close()
        assert root.handlers == mine, "the server left a handler on the root"
        assert root.level == level, "the server changed the log level"
        assert logging.getLogger("macrt").handlers, "the emulator lost its log"
        assert sdk.handlers and not sdk.propagate, "the SDK's own log has nowhere to go"
    finally:
        for name, handlers, lvl, prop in saved:
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.setLevel(lvl)
            logger.propagate = prop


def test_the_socket_is_quiet_unless_the_run_is_being_traced(tmp_path):
    """The emulator has one voice, and "Application startup complete" isn't
    something it should say between the game's lines -- but under --trace, what
    the socket is doing is exactly what someone wants to see.

    The console handler's level decides, not the logger's: macrt's logger is
    left at DEBUG on purpose so the run's log file takes everything."""
    from macrt import ansi
    saved = list(logging.getLogger("macrt").handlers)
    saved_level = logging.getLogger("macrt").level
    try:
        ansi.setup(None, logging.INFO)
        assert _quiet() == "WARNING"
        ansi.setup(str(tmp_path / "run.log"), logging.INFO)
        assert _quiet() == "WARNING", "the log file's DEBUG level isn't the screen's"
        ansi.setup(None, logging.DEBUG)
        assert _quiet() == "DEBUG"
    finally:
        for handler in logging.getLogger("macrt").handlers:
            handler.close()               # the file this run made, not the suite's
        logger = logging.getLogger("macrt")
        logger.handlers[:] = saved
        logger.setLevel(saved_level)


def test_the_commands_are_the_display_s_one_shared_with_the_window():
    """One dm page and one set of breakpoints between the window and the
    server, because there's one Commands on the display."""
    display = _Display(None)
    from macrt.mcpserver import Tools
    commands = Commands.for_display(display)
    assert Tools(display).commands is commands
    assert Commands.for_display(display) is commands


def test_a_port_already_taken_says_so_rather_than_quietly_taking_another():
    """The client's config names one URL.  An emulator that silently listened
    somewhere else would look connected and never answer there."""
    mem, cpu = machine()
    guest = Guest(cpu)
    first = McpServer(_Display(guest.debug), host="127.0.0.1", port=0)
    first.start()
    second = McpServer(_Display(guest.debug), host="127.0.0.1",
                       port=int(first.url.split(":")[2].split("/")[0]))
    try:
        assert first.started and first.url
        assert second.start() == ""           # the port was taken
        assert not second.started
        assert second.url == ""             # and offers no URL to put in a config
    finally:
        second.stop()
        first.stop()
        guest.close()


def test_no_server_is_left_listening_after_stop(server):
    mcp = server.mcp
    url = mcp.url
    assert _post(url, _initialize())[0] == 200
    mcp.stop()
    with pytest.raises(Exception):
        _post(url, _initialize(), timeout=2)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _initialize():
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "1"}}}


def _post(url, payload, headers=None, raw=None, method="POST", timeout=8):
    """One HTTP request, answered as (status, json).  A GET that stays open --
    which is what one does -- comes back as ("open", None) rather than hanging
    the suite."""
    body = raw if raw is not None else (
        None if payload is None else json.dumps(payload).encode())
    request = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return reply.status, _json(reply.read())
    except urllib.error.HTTPError as e:
        return e.code, _json(e.read())
    except TimeoutError:
        return "open", None


def _json(body: bytes):
    """The body, parsed if it's JSON.  A refusal from the transport itself --
    "Invalid Origin header" -- is plain prose, and that's the answer, so it
    comes back as text rather than failing the parse."""
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body.decode("utf-8", "replace")
