# -*- mode: python; indent-tabs-mode: nil -*-

# Part of mlat-client - an ADS-B multilateration client.
# Copyright 2015, Oliver Jowett <oliver@mutability.co.uk>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Core of the client: track aircraft and send data to the server as needed.
"""

import asyncore
import time

import mlat.profile
from mlat.client.util import monotonic_time, log
from mlat.client.stats import global_stats


class Aircraft:
    """One tracked aircraft."""

    def __init__(self, icao):
        self.icao = icao
        self.messages = 0
        self.last_message_time = 0
        self.last_position_time = 0
        self.even_message = None
        self.odd_message = None
        self.reported = False
        self.requested = True
        self.measurement_start = None
        self.rate_measurement_start = 0
        self.recent_adsb_positions = 0


class Coordinator:
    report_interval = 30.0
    stats_interval = 900.0

    def __init__(self, receiver, server, outputs, freq):
        self.receiver = receiver
        self.server = server
        self.outputs = outputs
        self.freq = freq

        self.aircraft = {}
        self.requested_traffic = set()
        self.df_handlers = {
            -1: self.received_clock_reset_marker,
            0: self.received_df_misc,
            4: self.received_df_misc,
            5: self.received_df_misc,
            16: self.received_df_misc,
            20: self.received_df_misc,
            21: self.received_df_misc,
            11: self.received_df11,
            17: self.received_df17
        }
        self.next_report = None
        self.next_stats = monotonic_time() + self.stats_interval
        self.next_profile = monotonic_time()

        receiver.coordinator = self
        server.coordinator = self

    # internals

    def run_forever(self):
        self.run_until(lambda: False)

    def run_until(self, termination_condition):
        try:
            next_heartbeat = monotonic_time() + 0.5
            while not termination_condition():
                # maybe there are no active sockets and
                # we're just waiting on a timeout
                if asyncore.socket_map:
                    asyncore.loop(timeout=0.1, count=5)
                else:
                    time.sleep(0.5)

                now = monotonic_time()
                if now >= next_heartbeat:
                    next_heartbeat = now + 0.5
                    self.heartbeat(now)

        finally:
            self.receiver.disconnect('Client shutting down')
            self.server.disconnect('Client shutting down')
            for o in self.outputs:
                o.disconnect()

    def heartbeat(self, now):
        self.receiver.heartbeat(now)
        self.server.heartbeat(now)
        for o in self.outputs:
            o.heartbeat(now)

        if now >= self.next_profile:
            self.next_profile = now + 30.0
            mlat.profile.dump_cpu_profiles()

        if self.next_report and now >= self.next_report:
            self.next_report = now + self.report_interval
            self.send_aircraft_report()
            self.expire(now)
            self.send_rate_report(now)

        if now >= self.next_stats:
            self.next_stats = now + self.stats_interval
            self.periodic_stats(now)

    def report_aircraft(self, ac):
        ac.reported = True
        self.newly_seen.add(ac.icao)

    def send_aircraft_report(self):
        if self.newly_seen:
            #log('Telling server about {0} new aircraft', len(self.newly_seen))
            self.server.send_seen(self.newly_seen)
            self.newly_seen.clear()

    def send_rate_report(self, now):
        # report ADS-B position rate stats
        rate_report = {}
        for ac in self.aircraft.values():
            interval = now - ac.rate_measurement_start
            if interval > 0 and (now - ac.last_position_time) < 60:
                rate = 1.0 * ac.recent_adsb_positions / interval
                ac.rate_measurement_start = now
                ac.recent_adsb_positions = 0
                rate_report[ac.icao] = rate

        if rate_report:
            self.server.send_rate_report(rate_report)

    def expire(self, now):
        discarded = []
        for ac in list(self.aircraft.values()):
            if (now - ac.last_message_time) > 60:
                if ac.reported:
                    discarded.append(ac.icao)
                del self.aircraft[ac.icao]

        if discarded:
            self.server.send_lost(discarded)

    def periodic_stats(self, now):
        log('Receiver connection: {0}', self.receiver.state)
        log('Server connection:   {0}', self.server.state)
        global_stats.log_and_reset()
        log('Aircraft: {0} known, {1} requested by server',
            len(self.aircraft), len(self.requested_traffic))

    # callbacks from server connection

    def server_connected(self):
        self.requested_traffic = set()
        self.newly_seen = set()
        self.aircraft = {}
        self.next_report = monotonic_time() + self.report_interval
        if self.receiver.state != 'ready':
            self.receiver.reconnect()

    def server_disconnected(self):
        self.receiver.disconnect('Lost connection to multilateration server, no need for input data')
        self.next_report = None
        self.next_rate_report = None
        self.next_expiry = None

    def server_mlat_result(self, timestamp, addr, lat, lon, alt, callsign, squawk, error_est, nstations):
        global_stats.mlat_positions += 1
        for o in self.outputs:
            o.send_position(timestamp, addr, lat, lon, alt, callsign, squawk, error_est, nstations)

    def server_start_sending(self, icao_list):
        for icao in icao_list:
            ac = self.aircraft.get(icao)
            if ac:
                ac.requested = True
        self.requested_traffic.update(icao_list)

    def server_stop_sending(self, icao_list):
        for icao in icao_list:
            ac = self.aircraft.get(icao)
            if ac:
                ac.requested = False
        self.requested_traffic.difference_update(icao_list)

    # callbacks from receiver input

    def input_connected(self):
        self.server.send_input_connected()

    def input_disconnected(self):
        self.server.send_input_disconnected()
        # expire everything
        discarded = list(self.aircraft.keys())
        self.aircraft.clear()
        self.server.send_lost(discarded)

    @mlat.profile.trackcpu
    def input_received_messages(self, messages):
        now = monotonic_time()
        for message in messages:
            handler = self.df_handlers.get(message.df)
            if handler:
                handler(message, now)

    # handlers for input messages

    def received_clock_reset_marker(self, message, now):
        # clock reset, but stream is intact
        self.server.send_clock_reset('Normal clock rollover (GPS start of day, etc)')

    def received_df_misc(self, message, now):
        ac = self.aircraft.get(message.address)
        if not ac:
            return False  # not a known ICAO

        ac.messages += 1
        ac.last_message_time = monotonic_time()

        if ac.messages < 10:
            return   # wait for more messages
        if not ac.reported:
            self.report_aircraft(ac)
            return
        if not ac.requested:
            return

        # Candidate for MLAT
        if now - ac.last_position_time < 60:
            return   # reported position recently, no need for mlat
        self.server.send_mlat(message)

    def received_df11(self, message, now):
        ac = self.aircraft.get(message.address)
        if not ac:
            ac = Aircraft(message.address)
            ac.requested = (message.address in self.requested_traffic)
            ac.messages += 1
            ac.last_message_time = now
            ac.rate_measurement_start = now
            self.aircraft[message.address] = ac
            return   # will need some more messages..

        ac.messages += 1
        ac.last_message_time = now

        if ac.messages < 10:
            return   # wait for more messages
        if not ac.reported:
            self.report_aircraft(ac)
            return
        if not ac.requested:
            return

        # Candidate for MLAT
        if now - ac.last_position_time < 60:
            return   # reported position recently, no need for mlat
        self.server.send_mlat(message)

    def received_df17(self, message, now):
        ac = self.aircraft.get(message.address)
        if not ac:
            ac = Aircraft(message.address)
            ac.requested = (message.address in self.requested_traffic)
            ac.messages += 1
            ac.last_message_time = now
            ac.rate_measurement_start = now
            self.aircraft[message.address] = ac
            return   # wait for more messages

        ac.messages += 1
        ac.last_message_time = now
        if ac.messages < 10:
            return
        if not ac.reported:
            self.report_aircraft(ac)
            return

        if not message.even_cpr and not message.odd_cpr:
            # not a position message
            return

        ac.last_position_time = now

        if message.altitude is None:
            return    # need an altitude
        if message.nuc < 6:
            return    # need NUCp >= 6

        ac.recent_adsb_positions += 1

        if self.server.send_split_sync:
            if not ac.requested:
                return

            # this is a useful reference message
            self.server.send_split_sync(message)
        else:
            if message.even_cpr:
                ac.even_message = message
            else:
                ac.odd_message = message

            if not ac.requested:
                return
            if not ac.even_message or not ac.odd_message:
                return
            if abs(ac.even_message.timestamp - ac.odd_message.timestamp) > 5 * self.freq:
                return

            # this is a useful reference message pair
            self.server.send_sync(ac.even_message, ac.odd_message)
