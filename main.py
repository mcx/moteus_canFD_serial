import argparse
import enum
import io
import math
import serial
import struct
import time

# These constants can be found in:
# https://github.com/mjbots/moteus/blob/master/docs/reference.md under
# "register command set".

MP_INT8 = 0
MP_INT16 = 1
MP_INT32 = 2
MP_F32 = 3

MP_WRITE_BASE = 0x00
MP_READ_BASE = 0x10
MP_REPLY_BASE = 0x20
MP_WRITE_ERROR = 0x30
MP_READ_ERROR = 0x31
MP_NOP = 0x50

_TYPE_STRUCTS = {
    MP_INT8: struct.Struct('<b'),
    MP_INT16: struct.Struct('<h'),
    MP_INT32: struct.Struct('<i'),
    MP_F32: struct.Struct('<f'),
}

MOTEUS_REG_MODE = 0x000
MOTEUS_REG_POSITION = 0x001
MOTEUS_REG_VELOCITY = 0x002
MOTEUS_REG_TORQUE = 0x003
MOTEUS_REG_Q_A = 0x004
MOTEUS_REG_D_A = 0x005
MOTEUS_REG_V = 0x00d
MOTEUS_REG_TEMP_C = 0x00e
MOTEUS_REG_FAULT = 0x00f

MOTEUS_REG_POS_POSITION = 0x20
MOTEUS_REG_POS_VELOCITY = 0x21
MOTEUS_REG_POS_TORQUE = 0x22
MOTEUS_REG_POS_KP = 0x23
MOTEUS_REG_POS_KD = 0x24
MOTEUS_REG_MAX_TORQUE = 0x25


class MoteusMode(enum.IntEnum):
    STOPPED = 0
    FAULT = 1
    PWM = 5
    VOLTAGE = 6
    VOLTAGE_FOC = 7
    VOLTAGE_DQ = 8
    CURRENT = 9
    POSITION = 10
    TIMEOUT = 11
    ZERO_VEL = 12


def hexify(data):
    return ''.join(['{:02x}'.format(x) for x in data])


def dehexify(data):
    result = b''
    for i in range(0, len(data), 2):
        result += bytes([int(data[i:i + 2], 16)])
    return result


def readline(stream):
    result = bytearray()
    while True:
        char = stream.read(1)
        if char == b'\n':
            if len(result):
                return result
        else:
            result += char


def read_varuint(stream):
    result = 0
    shift = 0

    for i in range(5):
        data = stream.read(1)
        if len(data) < 1:
            return None
        this_byte, = struct.unpack('<B', data)
        result |= (this_byte & 0x7f) << shift
        shift += 7

        if (this_byte & 0x80) == 0:
            return result

    assert False


def read_type(stream, field_type):
    s = _TYPE_STRUCTS[field_type]
    data = stream.read(s.size)
    return s.unpack(data)[0]


def parse_register_reply(data):
    stream = io.BytesIO(data)
    result = {}

    while True:
        opcode = read_varuint(stream)
        if opcode is None:
            break
        opcode_base = opcode & ~0x0f
        if opcode_base == MP_REPLY_BASE:
            field_type = (opcode & 0x0c) >> 2
            size = opcode & 0x03
            if size == 0:
                size = read_varuint(stream)
            start_reg = read_varuint(stream)
            for i in range(size):
                result[start_reg + i] = read_type(stream, field_type)
        elif opcode_base == MP_WRITE_ERROR:
            reg = read_varuint(stream)
            err = read_varuint(stream)
            result[reg] = 'werr {}'.format(err)
        elif opcode_base == MP_READ_ERROR:
            reg = read_varuint(stream)
            err = read_varuint(stream)
            result[reg] = 'rerr {}'.format(err)
        elif opcode_base == MP_NOP:
            pass
        else:
            # Unknown opcode.  Just bail.
            break

    return result


class Controller:
    def __init__(self, controller_ID):
        parser = argparse.ArgumentParser(description=__doc__)

        parser.add_argument('-d', '--device', type=str, default='/dev/fdcanusb',
                            help='serial device')
        parser.add_argument('-t', '--target', type=int, default=controller_ID,
                            help='ID of target device')
        args = parser.parse_args()

        self.serial = serial.Serial(port=args.device)
        self.target = args.target

        # Send a stop to begin with, in case we have a fault or
        # something.  The fault states are latching, and require a
        # stop command in order to make the device move again.
        self.command_stop()



    def send_can_frame(self, frame, reply, discard_adapter_response=True, print_data=False):
        self.serial.write("can send {:02x}{:02x} {}\n".format(
            0x80 if reply else 0x00,
            self.target, hexify(frame)).encode('latin1'))

        if(discard_adapter_response):
            # Read (and discard) the adapters response.
            ok_response = readline(self.serial)
            if not ok_response.startswith(b"OK"):
                raise RuntimeError("fdcanusb responded with: " +
                                   ok_response.decode('latin1'))

            if(reply):
                # Read the devices response.
                device = readline(self.serial)

                # if not device.startswith(b"rcv"):
                #     raise RuntimeError("unexpected response")

                fields = device.split(b" ")
                response = dehexify(fields[2])
                response_data = parse_register_reply(response)

                if(print_data):
                    print("Mode: {: 2d}  Pos: {: 6.2f}deg  Vel: {: 6.2f}dps  "
                          "Torque: {: 6.2f}Nm  Temp: {: 3d}C  Voltage: {: 3.1f}V    ".format(
                            int(response_data[MOTEUS_REG_MODE]),
                            response_data[MOTEUS_REG_POSITION] * 360.0,
                            response_data[MOTEUS_REG_VELOCITY] * 360.0,
                            response_data[MOTEUS_REG_TORQUE],
                            response_data[MOTEUS_REG_TEMP_C],
                            response_data[MOTEUS_REG_V] * 0.5))
                return response_data

    def command_stop(self):
        buf = io.BytesIO()
        buf.write(struct.pack(
            "<bbb",
            0x01,  # write int8 1x
            MOTEUS_REG_MODE,
            MoteusMode.STOPPED))

        self.send_can_frame(buf.getvalue(), reply=False)


    def command_position(self, position, velocity=0., max_torque=0.5, ff_torque=0., kp_scale=1., kd_scale=1.,
                         get_data=False, print_data=False):
        buf = io.BytesIO()
        buf.write(struct.pack(
            "<bbb",
            0x01,  # write int8 1x
            MOTEUS_REG_MODE,
            MoteusMode.POSITION))
        buf.write(struct.pack(
            "<bbbffffff",
            0x0c,
            6,  # write float32 6x
            MOTEUS_REG_POS_POSITION,
            position,
            velocity,
            ff_torque,
            kp_scale,
            kd_scale,
            max_torque,
        ))
        if(get_data):
            buf.write(struct.pack(
                "<bbb",
                0x1c,  # read float32 (variable number)
                4,  # 4 registers
                0x00  # starting at 0
            ))
            buf.write(struct.pack(
                "<bb",
                0x13,  # read int8 3x
                MOTEUS_REG_V))
            return self.send_can_frame(buf.getvalue(), reply=True, print_data=print_data)
        else:
            self.send_can_frame(buf.getvalue(), reply=False, print_data=print_data)


    def command_velocity(self, velocity=0., max_torque=0.5, ff_torque=0., kd_scale=1., get_data=False, print_data=False):
        buf = io.BytesIO()
        buf.write(struct.pack(
            "<bbb",
            0x01,  # write int8 1x
            MOTEUS_REG_MODE,
            MoteusMode.POSITION))
        buf.write(struct.pack(
            "<bbbffffff",
            0x0c,
            6,  # write float32 6x
            MOTEUS_REG_POS_POSITION,
            math.nan,
            velocity,
            ff_torque,
            0.,
            kd_scale,
            max_torque,
        ))
        if(get_data):
            buf.write(struct.pack(
                "<bbb",
                0x1c,  # read float32 (variable number)
                4,  # 4 registers
                0x00  # starting at 0
            ))
            buf.write(struct.pack(
                "<bb",
                0x13,  # read int8 3x
                MOTEUS_REG_V))

        self.send_can_frame(buf.getvalue(), reply=reply, print_data=print_data)

    def command_torque(self, torque=0., get_data=False, print_data=False):
        buf = io.BytesIO()
        buf.write(struct.pack(
            "<bbb",
            0x01,  # write int8 1x
            MOTEUS_REG_MODE,
            MoteusMode.POSITION))
        buf.write(struct.pack(
            "<bbbffffff",
            0x0c,
            6,  # write float32 6x
            MOTEUS_REG_POS_POSITION,
            math.nan,
            math.nan,
            torque,
            0.,
            0.,
            4.,
        ))
        if(get_data):
            buf.write(struct.pack(
                "<bbb",
                0x1c,  # read float32 (variable number)
                4,  # 4 registers
                0x00  # starting at 0
            ))
            buf.write(struct.pack(
                "<bb",
                0x13,  # read int8 3x
                MOTEUS_REG_V))

        self.send_can_frame(buf.getvalue(), reply=get_data, print_data=print_data)

    def get_data(self, print_data=False):
        buf = io.BytesIO()
        buf.write(struct.pack(
            "<bbb",
            0x1c,  # read float32 (variable number)
            4,  # 4 registers
            0x00  # starting at 0
        ))
        buf.write(struct.pack(
            "<bb",
            0x13,  # read int8 3x
            MOTEUS_REG_V))

        return self.send_can_frame(buf.getvalue(), reply=True, print_data=print_data)




def main():
    controller_1 = Controller(1)
    while True:
        phase = (time.time()*8) % (2. * math.pi);
        angle_deg = 200.0 / 360 * math.sin(phase)
        velocity_dps = 200/ 360 * math.cos(phase)
        torque_Nm=0.1*math.cos(phase)

        #controller_1.command_position(position=angle_deg, velocity=velocity_dps, max_torque=0.3, get_data=True, print_data=False)
        response_data=controller_1.get_data()
        devider = 45
        pos_set=(response_data[MOTEUS_REG_POSITION]*360-((response_data[MOTEUS_REG_POSITION] * 360)%(360/devider)-(360/devider/2)))
        print(pos_set)
        print(response_data[MOTEUS_REG_POSITION]*360)
        controller_1.command_position(position=pos_set/360,  max_torque=0.6, kd_scale=0.1, kp_scale=1)

        #controller_1.command_velocity(velocity=velocity_dps, max_torque=0.5)

        #controller_1.command_torque(torque=torque_Nm)



if __name__ == '__main__':
    main()
