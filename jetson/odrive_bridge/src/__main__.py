import lcm
import sys
import time as t
import odrive as odv
import threading
from rover_msgs import DriveVelCmd, \
    DriveStateData, DriveVelData
from odrive.enums import AXIS_STATE_CLOSED_LOOP_CONTROL, \
    CONTROL_MODE_VELOCITY_CONTROL, \
    AXIS_STATE_IDLE

from odrive.utils import dump_errors


def main():
    global lcm_
    lcm_ = lcm.LCM()

    global modrive
    global left_speed
    global right_speed

    left_speed = 0.0
    right_speed = 0.0

    global legal_controller

    global vel_msg
    global state_msg

    global lock
    global speedlock

    global start_time
    global watchdog

    start_time = t.clock()

    legal_controller = int(sys.argv[1])

    vel_msg = DriveVelData()
    state_msg = DriveStateData()

    speedlock = threading.Lock()
    lock = threading.Lock()

    threading._start_new_thread(lcmThreaderMan, ())
    global odrive_bridge
    odrive_bridge = OdriveBridge()
    # starting state is DisconnectedState()
    # start up sequence is called, disconnected-->disarm-->arm

    while True:
        watchdog = t.clock() - start_time
        if (watchdog > 1.0):
            print("loss of comms")

            speedlock.acquire()

            left_speed = 0
            right_speed = 0

            speedlock.release()

        try:
            odrive_bridge.update()
        except Exception as e:
            print("CRASH! Error: ")
            print(e)
            lock.acquire()
            odrive_bridge.on_event("disconnected odrive")
            lock.release()

    exit()


def lcmThreaderMan():
    lcm_1 = lcm.LCM()
    lcm_1.subscribe("/drive_vel_cmd", drive_vel_cmd_callback)
    while True:
        lcm_1.handle()
        global start_time
        start_time = t.clock()
        try:
            publish_encoder_msg()
        except NameError:
            pass
        except AttributeError:
            pass
        except Exception:
            pass


events = ["disconnected odrive", "disarm cmd", "arm cmd", "odrive error"]
states = ["DisconnectedState", "DisarmedState", "ArmedState", "ErrorState"]
# Program states possible - BOOT,  DISARMED, ARMED,   ERROR
# 							1		 2	      3	       4


class State(object):
    """
    State object which provides some utility functions for the
    individual states within the state machine.
    """

    def __init__(self):
        print('Processing current state:', str(self))

    def on_event(self, event):
        """
        Handle events that are delegated to this State.
        """
        pass

    def __repr__(self):
        """
        Make it so __str__ method can describe the State.
        """
        return self.__str__()

    def __str__(self):
        """
        Returns the name of the State.
        State state
        str(state) = State
        """
        return self.__class__.__name__


class DisconnectedState(State):
    def on_event(self, event):
        """
        Handle events that are delegated to the Disconnected State.
        """
        global modrive
        try:
            if (event == "arm cmd"):
                modrive.disarm()
                modrive.reset_watchdog()
                modrive.arm()
                return ArmedState()
        except:
            print("trying to arm")

        return self


class DisarmedState(State):
    def on_event(self, event):
        """
        Handle events that are delegated to the Disarmed State.
        """
        global modrive
        if (event == "disconnected odrive"):
            return DisconnectedState()

        elif (event == "arm cmd"):
            modrive.arm()
            return ArmedState()

        elif (event == "odrive error"):
            return ErrorState()

        return self


class ArmedState(State):
    def on_event(self, event):
        """
        Handle events that are delegated to the Armed State.
        """
        global modrive

        if (event == "disarm cmd"):
            modrive.disarm()
            return DisarmedState()

        elif (event == "disconnected odrive"):
            global speedlock
            global left_speed
            global right_speed
            speedlock.acquire()
            left_speed = 0
            right_speed = 0
            speedlock.release()

            return DisconnectedState()

        elif (event == "odrive error"):
            return ErrorState()

        return self


class ErrorState(State):
    def on_event(self, event):
        """
        Handle events that are delegated to the Error State.
        """
        global modrive
        dump_errors(modrive.odrive, True)

        if (event == "odrive error"):
            try:
                modrive.reboot()  # only runs after initial pairing
            except:
                print('channel error caught')
            return DisconnectedState()

        return self


class OdriveBridge(object):

    def __init__(self):
        """
        Initialize the components.
        Start with a Default State
        """
        global modrive
        self.state = DisconnectedState()  # default is disarmed
        self.encoder_time = 0
        self.errors = 0
        self.left_speed = 0
        self.right_speed = 0

    def connect(self):
        global modrive
        global legal_controller
        print("looking for odrive")

        # odrive 0 --> front motors
        # odrive 1 --> middle motors
        # odrive 2 --> back motors

        odrives = ["335D36623539", "335B36563539", "335536553539"]
        id = odrives[legal_controller]

        print(id)
        odrive = odv.find_any(serial_number=id)

        print("found odrive")
        modrive = Modrive(odrive)  # arguments = odr
        modrive.set_current_lim(100)
        self.encoder_time = t.time()

    def on_event(self, event):
        """
        Incoming events are
        delegated to the given states which then handle the event.
        The result is then assigned as the new state.
        The events we can send are disarm cmd, arm cmd, and calibrating cmd.
        """

        print("on event called, event:", event)

        self.state = self.state.on_event(event)
        publish_state_msg(state_msg, odrive_bridge.get_state())

    def update(self):
        try:
            errors = modrive.check_errors()
            modrive.watchdog_feed()
        except Exception:
            errors = 0
            lock.acquire()
            self.on_event("disconnected odrive")
            lock.release()
            print("unable to check errors of unplugged odrive")

        if errors:
            # if (errors == 0x800 or erros == 0x1000):

            lock.acquire()
            self.on_event("odrive error")
            lock.release()
            # first time will set to ErrorState
            # second time will reboot
            # because the error flag is still true
            return

        if (str(self.state) == "ArmedState"):
            modrive.watchdog_feed()

            global speedlock
            global left_speed
            global right_speed

            # print("trying to acquire speed lock in update")
            speedlock.acquire()
            # print("acquired speed lock in update")
            self.left_speed = left_speed
            self.right_speed = right_speed

            # print("released speed lock in update")
            speedlock.release()

            modrive.set_vel("LEFT", self.left_speed)
            modrive.set_vel("RIGHT", self.right_speed)

        elif (str(self.state) == "DisconnectedState"):
            self.connect()
            lock.acquire()
            self.on_event("arm cmd")
            lock.release()

        try:
            errors = modrive.check_errors()
        except Exception:
            errors = 0
            lock.acquire()
            self.on_event("disconnected odrive")
            lock.release()
            print("unable to check errors of unplugged odrive")

        if errors:
            # if (errors == 0x800 or erros == 0x1000):

            lock.acquire()
            self.on_event("odrive error")
            lock.release()
            # first time will set to ErrorState
            # second time will reboot
            # because the error flag is still true

    def get_state(self):
        return str(self.state)


"""
call backs
"""


def publish_state_msg(msg, state):
    global legal_controller
    msg.state = states.index(state)
    msg.controller = legal_controller
    lcm_.publish("/drive_state_data", msg.encode())
    print("changed state to " + state)


def publish_encoder_helper(axis):
    global modrive
    global legal_controller
    msg = DriveVelData()
    msg.measuredCurrent = modrive.get_iq_measured(axis)
    msg.estimatedVel = modrive.get_vel_estimate(axis)

    motor_map = {("LEFT", 0): 0, ("RIGHT", 0): 1,
                 ("LEFT", 1): 2, ("RIGHT", 1): 3,
                 ("LEFT", 2): 4, ("RIGHT", 2): 5}

    msg.axis = motor_map[(axis, legal_controller)]

    lcm_.publish("/drive_vel_data", msg.encode())


def publish_encoder_msg():
    publish_encoder_helper("LEFT")
    publish_encoder_helper("RIGHT")


def drive_vel_cmd_callback(channel, msg):
    # set the odrive's velocity to the float specified in the message
    # no state change

    global speedlock
    global odrive_bridge
    try:
        cmd = DriveVelCmd.decode(msg)
        if (odrive_bridge.get_state() == "ArmedState"):
            global left_speed
            global right_speed

            # print("trying to acquire speed lock in drive vel callback")
            speedlock.acquire()
            # print("speed lock acquired in drive call back")

            left_speed = cmd.left
            right_speed = cmd.right
            # print("speed lock released in drive call back")
            speedlock.release()
    except NameError:
        pass


if __name__ == "__main__":
    main()


class Modrive:
    CURRENT_LIM = 30

    def __init__(self, odr):
        self.odrive = odr
        self.front_axis = self.odrive.axis0
        self.back_axis = self.odrive.axis1
        self.set_current_lim(self.CURRENT_LIM)
        # TODO fix this such that front and back are right and left

    # viable to set initial state to idle?

    def __getattr__(self, attr):
        if attr in self.__dict__:
            return getattr(self, attr)
        return getattr(self.odrive, attr)

    def reset(self):
        self._reset(self.front_axis)
        self._reset(self.back_axis)
        self.odrive.save_configuration()
        # the guide says to reboot here...

    def print_debug(self):
        try:
            print("Print control mode")
            print(self.front_axis.controller.config.control_mode)
            print(self.back_axis.controller.config.control_mode)
            print("Printing requested state")
            print(self.front_axis.current_state)
            print(self.back_axis.current_state)
        except Exception as e:
            print("Failed in print_debug. Error:")
            print(e)

    def enable_watchdog(self):
        try:
            print("Enabling watchdog")
            self.front_axis.config.watchdog_timeout = 0.1
            self.back_axis.config.watchdog_timeout = 0.1
            self.watchdog_feed()
            self.front_axis.config.enable_watchdog = True
            self.back_axis.config.enable_watchdog = True
        except Exception as e:
            print("Failed in enable_watchdog. Error:")
            print(e)

    def disable_watchdog(self):
        try:
            print("Disabling watchdog")
            self.front_axis.config.watchdog_timeout = 0
            self.back_axis.config.watchdog_timeout = 0
            self.front_axis.config.enable_watchdog = False
            self.back_axis.config.enable_watchdog = False
        except Exception as e:
            print("Failed in disable_watchdog. Error:")
            print(e)

    def reset_watchdog(self):
        try:
            print("Resetting watchdog")
            self.disable_watchdog()
            # clears errors cleanly
            self.odrive.clear_errors()
            self.enable_watchdog()
        except Exception as e:
            print("Failed in disable_watchdog. Error:")
            print(e)

    def watchdog_feed(self):
        try:
            self.front_axis.watchdog_feed()
            self.back_axis.watchdog_feed()
        except Exception as e:
            print("Failed in watchdog_feed. Error:")
            print(e)

    def disarm(self):
        self.closed_loop_ctrl()
        self.set_velocity_ctrl()

        self.set_vel("LEFT", 0)
        self.set_vel("RIGHT", 0)

        self.idle()

    def arm(self):
        self.closed_loop_ctrl()
        self.set_velocity_ctrl()

    def set_current_lim(self, lim):
        self.front_axis.motor.config.current_lim = lim
        self.back_axis.motor.config.current_lim = lim

    def _set_control_mode(self, mode):
        self.front_axis.controller.config.control_mode = mode
        self.back_axis.controller.config.control_mode = mode

    def set_velocity_ctrl(self):
        self._set_control_mode(CONTROL_MODE_VELOCITY_CONTROL)

    def get_iq_measured(self, axis):
        # measured current [Amps]
        if (axis == "LEFT"):
            return self.front_axis.motor.current_control.Iq_measured
        elif(axis == "RIGHT"):
            return self.back_axis.motor.current_control.Iq_measured

    def get_vel_estimate(self, axis):
        # axis = self.odrive[axis_number]
        if (axis == "LEFT"):
            return self.front_axis.encoder.vel_estimate
        elif(axis == "RIGHT"):
            return self.back_axis.encoder.vel_estimate

    def idle(self):
        self._requested_state(AXIS_STATE_IDLE)

    def closed_loop_ctrl(self):
        self._requested_state(AXIS_STATE_CLOSED_LOOP_CONTROL)

    def _requested_state(self, state):
        self.back_axis.requested_state = state
        self.front_axis.requested_state = state

    def set_vel(self, axis, vel):
        global legal_controller
        if (axis == "LEFT"):
            # TEMPORARY FIX FOR ROLLING ROVER SINCE
            # middle left odrive IS 2x more than the rest bc of the 48V maxon
            # TODO - fix when this is no longer the case!
            if (legal_controller == 1):
                self.front_axis.controller.input_vel = vel * 100
            else:
                self.front_axis.controller.input_vel = vel * 50
        elif axis == "RIGHT":
            self.back_axis.controller.input_vel = vel * -50

    def get_current_state(self):
        return (self.front_axis.current_state, self.back_axis.current_state)

    def _pre_calibrate(self, m_axis):
        m_axis.motor.config.pre_calibrated = True
        m_axis.encoder.config.pre_calibrated = True

    def check_errors(self):
        return self._check_error_on_axis(self.front_axis) + \
                                self._check_error_on_axis(self.back_axis)

    def _check_error_on_axis(self, axis):
        return axis.error + axis.encoder.error + axis.controller.error + axis.motor.error
