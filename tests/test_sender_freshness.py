"""A newer screen colour should supersede repeats of an older colour."""

import threading

from src.features import fast_udp_sender as udp


def test_new_colour_interrupts_repeats_of_an_old_colour(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.packets = []
            self.first_sent = threading.Event()
            self.new_sent = threading.Event()

        def send(self, packet):
            self.packets.append(packet)
            self.first_sent.set()
            if packet == b"new":
                self.new_sent.set()

        def close(self):
            pass

    socket = FakeSocket()
    monkeypatch.setattr(udp.BulbUDPSocket, "_connect", lambda self: socket)
    monkeypatch.setattr(udp, "_BURST_GAP", 0.1)
    sender = udp.BulbUDPSocket("synthetic", max_send_hz=1000)
    try:
        sender.send(b"old")
        assert socket.first_sent.wait(2)
        sender.send(b"new")
        assert socket.new_sent.wait(2)
    finally:
        sender.stop()
    assert socket.packets[:2] == [b"old", b"new"]
