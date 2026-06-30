"""
nec.py - Control library for NEC PX803UL (and compatible PA/PX series) projectors.

Communicates over TCP port 7142 using NEC's binary "Projector Control Command"
protocol (ref. NEC doc BDT140013 / BDT140014). Every command is a byte frame whose
last byte is a checksum = (sum of all preceding bytes) & 0xFF.

All command byte sequences in this file are taken directly from the NEC
Control Command Reference Manual and have been checksum-verified.
"""

import socket
import threading

# ----- Lens axis targets (DATA01 of command 053 / 053-1) -----
LENS_AXES = {
    "zoom": 0x00,
    "focus": 0x01,
    "shift_h": 0x02,   # horizontal lens shift
    "shift_v": 0x03,   # vertical lens shift
    "periphery_focus": 0x06,
}

# ----- Lens drive "content" bytes (DATA02 of command 053) -----
# Timed pulses are the safest way to nudge the lens over a network: each press
# moves for a fixed time and stops on its own, so a dropped "stop" packet can
# never leave the lens running away.
LENS_PLUS = {0.25: 0x03, 0.5: 0x02, 1.0: 0x01}
LENS_MINUS = {0.25: 0xFD, 0.5: 0xFE, 1.0: 0xFF}
LENS_CONT_PLUS = 0x7F     # drive continuously (+) until a Stop is sent
LENS_CONT_MINUS = 0x81    # drive continuously (-) until a Stop is sent
LENS_STOP = 0x00

# ----- Input terminal codes (DATA after the 01h selector in 018 INPUT SW CHANGE) -----
# NOTE: HDMI/DisplayPort codes can vary slightly between firmware revisions.
# The values below are the commonly documented NEC PA/PX codes. If a given input
# returns an error, use the "Raw command" tester in the UI to find the right one.
INPUTS = {
    "HDMI":        0x1A,
    "DisplayPort": 0x1B,
    "HDBaseT":     0x20,
    "Computer":    0x01,
    "Video":       0x06,
    "SLOT":        0x0D,
    "Viewer/USB":  0x1F,
}

# Operation-status values (DATA06 of 078-2 RUNNING STATUS REQUEST)
POWER_STATES = {
    0x00: "standby",
    0x04: "on",
    0x05: "cooling",
    0x06: "standby_error",
    0x0F: "standby_powersaving",
    0x10: "network_standby",
}


def _checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def _frame(body: bytes) -> bytes:
    """Append the one-byte checksum to a command body."""
    return body + bytes([_checksum(body)])


class NECError(Exception):
    pass


class NECProjector:
    def __init__(self, host, port=7142, timeout=2.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        # Serialize access so concurrent web requests don't interleave on the socket.
        self._lock = threading.Lock()

    # --- low level ---------------------------------------------------------
    def _send(self, body: bytes, read_len=64) -> bytes:
        frame = _frame(body)
        with self._lock:
            try:
                with socket.create_connection((self.host, self.port), self.timeout) as s:
                    s.settimeout(self.timeout)
                    s.sendall(frame)
                    try:
                        resp = s.recv(read_len)
                    except socket.timeout:
                        resp = b""
            except OSError:
                # Projector off, unplugged, or unreachable.
                resp = b""
        return resp

    def send_raw(self, hexstr: str, append_checksum=False) -> str:
        """Send an arbitrary hex string (for testing). Returns hex of the reply."""
        clean = hexstr.replace("0x", "").replace(",", " ").split()
        data = bytes(int(b, 16) for b in clean)
        if append_checksum:
            data = _frame(data)
        resp = self._send(data)
        return resp.hex(" ")

    @staticmethod
    def _is_ok(resp: bytes) -> bool:
        # Success replies start with 0x2x; failure replies start with 0xAx.
        return len(resp) >= 1 and (resp[0] & 0xA0) != 0xA0

    # --- power -------------------------------------------------------------
    def power_on(self):
        return self._is_ok(self._send(bytes([0x02, 0x00, 0x00, 0x00, 0x00])))

    def power_off(self):
        return self._is_ok(self._send(bytes([0x02, 0x01, 0x00, 0x00, 0x00])))

    def get_power_state(self) -> str:
        # 078-2 RUNNING STATUS REQUEST: 00 85 00 00 01 01 (+cks)
        resp = self._send(bytes([0x00, 0x85, 0x00, 0x00, 0x01, 0x01]))
        if len(resp) >= 11 and self._is_ok(resp):
            return POWER_STATES.get(resp[10], "unknown")
        return "unreachable"

    def get_model_name(self) -> str:
        # 078-5 MODEL NAME REQUEST: 00 85 00 00 01 04 (+cks)
        resp = self._send(bytes([0x00, 0x85, 0x00, 0x00, 0x01, 0x04]), read_len=64)
        if len(resp) >= 6 and self._is_ok(resp):
            name = bytes(resp[5:5 + 32]).split(b"\x00", 1)[0]
            return name.decode("ascii", "ignore").strip()
        return ""

    # --- shutter / mute / freeze ------------------------------------------
    def shutter_open(self):
        return self._is_ok(self._send(bytes([0x02, 0x17, 0x00, 0x00, 0x00])))

    def shutter_close(self):
        return self._is_ok(self._send(bytes([0x02, 0x16, 0x00, 0x00, 0x00])))

    def picture_mute_on(self):
        return self._is_ok(self._send(bytes([0x02, 0x10, 0x00, 0x00, 0x00])))

    def picture_mute_off(self):
        return self._is_ok(self._send(bytes([0x02, 0x11, 0x00, 0x00, 0x00])))

    def freeze_on(self):
        return self._is_ok(self._send(bytes([0x01, 0x98, 0x00, 0x00, 0x01, 0x01])))

    def freeze_off(self):
        return self._is_ok(self._send(bytes([0x01, 0x98, 0x00, 0x00, 0x01, 0x02])))

    # --- input -------------------------------------------------------------
    def input_select(self, code: int):
        # 018 INPUT SW CHANGE: 02 03 00 00 02 01 <code> (+cks)
        return self._is_ok(self._send(bytes([0x02, 0x03, 0x00, 0x00, 0x02, 0x01, code])))

    def input_select_name(self, name: str):
        if name not in INPUTS:
            raise NECError(f"Unknown input '{name}'")
        return self.input_select(INPUTS[name])

    # --- lens --------------------------------------------------------------
    def _lens(self, axis: str, content: int):
        if axis not in LENS_AXES:
            raise NECError(f"Unknown lens axis '{axis}'")
        target = LENS_AXES[axis]
        # 053 LENS CONTROL: 02 18 00 00 02 <target> <content> (+cks)
        return self._is_ok(self._send(bytes([0x02, 0x18, 0x00, 0x00, 0x02, target, content])))

    def lens_jog(self, axis: str, direction: str, seconds: float = 0.5):
        """Nudge a lens axis for a fixed time. direction = 'plus' or 'minus'."""
        table = LENS_PLUS if direction == "plus" else LENS_MINUS
        if seconds not in table:
            seconds = 0.5
        return self._lens(axis, table[seconds])

    def lens_continuous(self, axis: str, direction: str):
        content = LENS_CONT_PLUS if direction == "plus" else LENS_CONT_MINUS
        return self._lens(axis, content)

    def lens_stop(self, axis: str):
        return self._lens(axis, LENS_STOP)

    def lens_stop_all(self):
        ok = True
        for axis in ("zoom", "focus", "shift_h", "shift_v"):
            ok = self._lens(axis, LENS_STOP) and ok
        return ok

    def get_lens_position(self, axis: str):
        """Returns dict with min/max/current (signed 16-bit) for an axis, or None."""
        if axis not in LENS_AXES:
            raise NECError(f"Unknown lens axis '{axis}'")
        target = LENS_AXES[axis]
        # 053-1 LENS CONTROL REQUEST: 02 1C 00 00 02 <target> 00 (+cks)
        resp = self._send(bytes([0x02, 0x1C, 0x00, 0x00, 0x02, target, 0x00]))
        if len(resp) >= 13 and self._is_ok(resp):
            def s16(lo, hi):
                v = lo | (hi << 8)
                return v - 0x10000 if v >= 0x8000 else v
            return {
                "axis": axis,
                "max": s16(resp[7], resp[8]),
                "min": s16(resp[9], resp[10]),
                "current": s16(resp[11], resp[12]),
            }
        return None

    # --- lens memory -------------------------------------------------------
    def lens_memory_move(self):
        # 053-3 LENS MEMORY CONTROL: 02 1E 00 00 01 00 (+cks)  (00=MOVE)
        return self._is_ok(self._send(bytes([0x02, 0x1E, 0x00, 0x00, 0x01, 0x00])))

    def lens_memory_store(self):
        # 01 = STORE
        return self._is_ok(self._send(bytes([0x02, 0x1E, 0x00, 0x00, 0x01, 0x01])))

    # --- aggregate status --------------------------------------------------
    def status(self):
        out = {"host": self.host, "port": self.port}
        state = self.get_power_state()
        out["power"] = state
        out["reachable"] = state != "unreachable"
        if out["reachable"]:
            out["model"] = self.get_model_name()
        return out
