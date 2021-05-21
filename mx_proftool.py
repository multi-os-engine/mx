#
# ----------------------------------------------------------------------------------------------------

# Copyright (c) 2021, Oracle and/or its affiliates. All rights reserved.
# DO NOT ALTER OR REMOVE COPYRIGHT NOTICES OR THIS FILE HEADER.
#
# This code is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 only, as
# published by the Free Software Foundation.
#
# This code is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# version 2 for more details (a copy is included in the LICENSE file that
# accompanied this code).
#
# You should have received a copy of the GNU General Public License version
# 2 along with this work; if not, write to the Free Software Foundation,
# Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Please contact Oracle, 500 Oracle Parkway, Redwood Shores, CA 94065 USA
# or visit www.oracle.com if you need additional information or have any
# questions.
#
# ----------------------------------------------------------------------------------------------------


from __future__ import print_function

import copy
import io
import shutil
import subprocess
import struct

import mx
import mx_benchmark

import os
import re
from argparse import ArgumentParser
from zipfile import ZipFile

try:
    # import into the global scope but don't complain if it's not there.  The commands themselves
    # will perform the check again and produce a helpful error message if it's not available.
    import capstone
except ImportError:
    pass


def check_capstone_import(name):
    try:
        import capstone  # pylint: disable=unused-variable, unused-import
    except ImportError as e:
        mx.abort(
            '{}\nThe capstone module is required to support \'{}\'. Try installing it with `pip install capstone`'.format(
                e, name))


class ProftoolProfiler(mx_benchmark.JVMProfiler):
    """
    Use perf on linux and a JVMTI agent to capture Java profiles.
    """

    def name(self):
        return "proftool"

    def version(self):
        return "1.0"

    def libraryPath(self):
        return find_jvmti_asm_agent()

    def sets_vm_prefix(self):
        return True

    def additional_options(self, dump_path):
        if not self.nextItemName:
            return [], []
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        if self.nextItemName:
            directory = os.path.join(dump_path, "proftool_{}_{}".format(self.nextItemName, timestamp))
        else:
            directory = os.path.join(dump_path, "proftool_{}".format(timestamp))
        files = FlatExperimentFiles.create(directory, overwrite=True)
        perf_cmd, vm_args = build_capture_args(files)

        # reset the next item name since it has just been consumed
        self.nextItemName = None
        return vm_args, perf_cmd


try:
    mx_benchmark.register_profiler(ProftoolProfiler())
except AttributeError:
    mx.warn('proftool unable to register profiler')

# File header format
filetag = b"JVMTIASM"
MajorVersion = 1
MinorVersion = 0

# Marker values for various data sections
DynamicCodeTag, = struct.unpack('>i', b'DYNC')
CompiledMethodLoadTag, = struct.unpack('>i', b'CMLT')
MethodsTag, = struct.unpack('>i', b'MTHT')
DebugInfoTag, = struct.unpack('>i', b'DEBI')
CompiledMethodUnloadTag, = struct.unpack('>i', b'CMUT')


class ExperimentFiles(object):
    """A collection of data files from a performance data collection experiment."""

    def __init__(self):
        pass

    @staticmethod
    def open(options):
        options_dict = vars(options)
        if options_dict.get('experiment'):
            experiment = options_dict.get('experiment')
            if os.path.isdir(experiment):
                return FlatExperimentFiles(directory=experiment)
            else:
                return ZipExperimentFiles(experiment)
        else:
            return FlatExperimentFiles(jvmti_asm_name=options_dict.get('jvmti_asm_file'),
                                       perf_binary_name=options_dict.get('perf_binary_file'))

    def open_jvmti_asm_file(self):
        raise NotImplementedError()

    def has_assembly(self):
        raise NotImplementedError()

    def get_jvmti_asm_filename(self):
        raise NotImplementedError()

    def get_perf_binary_filename(self):
        raise NotImplementedError()

    def get_perf_output_filename(self):
        raise NotImplementedError()

    def open_perf_output_file(self, mode='r'):
        raise NotImplementedError()

    def package(self, name=None):
        raise NotImplementedError()


class FlatExperimentFiles(ExperimentFiles):
    """A collection of data files from a performance data collection experiment."""

    def __init__(self, directory=None, jvmti_asm_name='jvmti_asm_file', perf_binary_name='perf_binary_file',
                 perf_output_name='perf_output_file'):
        super(FlatExperimentFiles, self).__init__()
        self.dump_path = None
        if directory:
            self.directory = os.path.abspath(directory)
            if not os.path.isdir(directory):
                raise AssertionError('Must be directory')
            self.jvmti_asm_filename = os.path.join(directory, jvmti_asm_name)
            self.perf_binary_filename = os.path.join(directory, perf_binary_name)
            self.perf_output_filename = os.path.join(directory, perf_output_name)
        else:
            self.directory = None
            self.jvmti_asm_filename = jvmti_asm_name
            self.perf_binary_filename = perf_binary_name
            self.perf_output_filename = perf_output_name

    @staticmethod
    def create(experiment, overwrite=False):
        experiment = os.path.abspath(experiment)
        if os.path.exists(experiment):
            if not overwrite:
                mx.abort('Experiment file already exists: {}'.format(experiment))
            shutil.rmtree(experiment)
        os.mkdir(experiment)
        return FlatExperimentFiles(directory=experiment)

    def open_jvmti_asm_file(self):
        return open(self.jvmti_asm_filename, 'rb')

    def has_assembly(self):
        return self.jvmti_asm_filename and os.path.exists(self.jvmti_asm_filename)

    def open_perf_output_file(self, mode='r'):
        return open(self.perf_output_filename, mode)

    def get_jvmti_asm_filename(self):
        return self.jvmti_asm_filename

    def get_perf_binary_filename(self):
        return self.perf_binary_filename

    def has_perf_binary(self):
        return self.perf_binary_filename and os.path.exists(self.perf_binary_filename)

    def get_perf_output_filename(self):
        return self.perf_output_filename

    def has_perf_output(self):
        return self.perf_output_filename and os.path.exists(self.perf_output_filename)

    def create_dump_dir(self):
        if self.dump_path:
            return self.dump_path
        if self.directory:
            self.dump_path = os.path.join(self.directory, 'dump')
            os.mkdir(self.dump_path)
            return self.dump_path
        else:
            raise AssertionError('Unhandled')

    def package(self, name=None):
        if self.directory:
            if not self.has_perf_output():
                convert_cmd = PerfOutput.perf_convert_binary_command(self)
                # convert the perf binary data into text format
                with self.open_perf_output_file(mode='w') as fp:
                    mx.run(convert_cmd, out=fp)

            directory_name = os.path.basename(self.directory)
            parent = os.path.dirname(self.directory)
            if not name:
                name = directory_name
            return shutil.make_archive(name, 'zip', root_dir=parent, base_dir=directory_name)
        else:
            raise AssertionError('Unhandled')


class ZipExperimentFiles(ExperimentFiles):
    """A collection of data files from a performance data collection experiment."""

    def __init__(self, filename):
        super(ZipExperimentFiles, self).__init__()
        self.experiment_file = ZipFile(filename)
        self.jvmti_asm_file = self.find_file('jvmti_asm_file')
        self.perf_output_filename = self.find_file('perf_output_file')

    def find_file(self, name):
        for f in self.experiment_file.namelist():
            if f.endswith(os.sep + name):
                return f
        mx.abort('Missing file ' + name)

    def open_jvmti_asm_file(self):
        return self.experiment_file.open(self.jvmti_asm_file, 'r')

    def has_assembly(self):
        return 'jvmti_asm_file' in self.experiment_file.namelist()

    def open_perf_output_file(self, mode='r'):
        return io.TextIOWrapper(self.experiment_file.open(self.perf_output_filename, mode), encoding='utf-8')

    def get_jvmti_asm_filename(self):
        mx.abort('Unable to output directly to zip file')

    def get_perf_binary_filename(self):
        mx.abort('Unable to output directly to zip file')


class Instruction:
    """A simple wrapper around a CapStone instruction to support data instructions."""

    def __init__(self, address, mnemonic, operand, instruction_bytes, size, insn=None):
        self.address = address
        self.mnemonic = mnemonic
        self.operand = operand
        self.bytes = instruction_bytes
        self.size = size
        self.insn = insn
        self.prefix = None
        self.comments = None

    def groups(self):
        if self.insn and self.insn.groups:
            return [self.insn.group_name(g) for g in self.insn.groups]
        return []


class DisassemblyBlock:
    """A chunk of disassembly with associated annotations"""

    def __init__(self, instructions):
        self.instructions = instructions


class DisassemblyDecoder:
    """A lightweight wrapper around the CapStone disassembly provide some extra functionality."""

    def __init__(self, decoder):
        decoder.detail = True
        self.decoder = decoder
        self.annotators = []
        self.hex_bytes = False

    def add_annotator(self, annotator):
        self.annotators.append(annotator)

    def successors(self, instruction):
        raise NotImplementedError()

    def disassemble_with_skip(self, code, code_addr):
        instructions = [Instruction(i.address, i.mnemonic, i.op_str, i.bytes, i.size, i) for i in
                        self.decoder.disasm(code, code_addr)]
        total_size = len(code)
        if instructions:
            last = instructions[-1]
            decoded_bytes = last.address + last.size - code_addr
        else:
            decoded_bytes = 0
        while decoded_bytes != total_size:
            new_instructions = [Instruction(i.address, i.mnemonic, i.op_str, i.bytes, i.size, i) for i in
                                self.decoder.disasm(code[decoded_bytes:], code_addr + decoded_bytes)]
            if new_instructions:
                instructions.extend(new_instructions)
                last = instructions[-1]
                decoded_bytes = last.address + last.size - code_addr
            else:
                instructions.append(Instruction(code_addr + decoded_bytes, '.byte', '{:0x}'.format(code[decoded_bytes]),
                                                code[decoded_bytes:decoded_bytes + 1], 1))
                decoded_bytes += 1
        return instructions

    def find_jump_targets(self, instructions):
        targets = set()
        for i in instructions:
            if i.insn:
                successors = self.successors(i.insn)
                if successors:
                    for successor in successors:
                        if successor:
                            targets.add(successor)
        targets = list(targets)
        targets.sort()
        return targets

    def get_annotations(self, instruction):
        preannotations = []
        postannotations = []
        for x in self.annotators:
            a = x(instruction)
            if a is None:
                continue
            if isinstance(a, list):
                postannotations.extend(a)
            elif isinstance(a, tuple):
                post, pre = x(instruction)
                if pre:
                    preannotations.append(pre)
                if post:
                    postannotations.extend(post)
            elif isinstance(a, str):
                postannotations.append(a)
            else:
                message = 'Unexpected annotation: {}'.format(a)
                mx.abort(message)
        return preannotations[0] if preannotations else None, postannotations

    def filter_by_hot_region(self, instructions, hotpc, context_size=16):
        index = 0
        begin = None
        skip = 0
        regions = []
        for instruction in instructions:
            if instruction.address in hotpc:
                skip = 0
                if not begin:
                    begin = max(index - context_size, 0)
                hotpc.remove(instruction.address)
            else:
                skip += 1
            if begin and skip > context_size:
                regions.append((begin, index))
                begin = None
                skip = 0
            index += 1
        if begin:
            regions.append((begin, index))
        if len(hotpc) != 0:
            print('Unattributed pcs {}'.format(['{:x}'.format(x) for x in list(hotpc)]))
        return regions

    def disassemble(self, code, code_addr, hotpc, show_regions=False):
        instructions = self.disassemble_with_skip(code, code_addr)
        regions = self.filter_by_hot_region(instructions, hotpc)
        if not show_regions:
            regions = [(0, len(instructions))]
        instructions = [(i,) + self.get_annotations(i) for i in instructions]
        prefix_width = max(len(p) if p else 0 for i, p, a in instructions) + 1
        prefix_format = '{:' + str(prefix_width) + '}'
        region = 1

        for begin, end in regions:
            if show_regions:
                print("Hot region {}".format(region))
            for i, prefix, annotations in instructions[begin:end]:
                hex_bytes = ''
                if self.hex_bytes:
                    hex_bytes = ' '.join(['{:02x}'.format(b) for b in i.bytes])
                if prefix is None:
                    prefix = ' ' * prefix_width
                else:
                    prefix = prefix_format.format(prefix)
                assert len(prefix) == prefix_width, '{} {}'.format(prefix, prefix_width)
                line = '{}0x{:x}:\t{}\t{}\t{}'.format(prefix, i.address, i.mnemonic, i.operand, hex_bytes)
                line = line.expandtabs()
                if annotations:
                    padding = ' ' * len(line)
                    lines = [padding] * len(annotations)
                    lines[0] = line
                    for a, b in zip(lines, annotations):
                        print('{}; {}'.format(a, b))
                else:
                    print(line)
            if show_regions:
                print("End of hot region {}".format(region))
            print('')
            region += 1

        last, _, _ = instructions[-1]
        decode_end = last.address + last.size
        buffer_end = code_addr + len(code)
        if decode_end != buffer_end:
            print('Skipping {} bytes {:x} {:x} '.format(buffer_end - decode_end, buffer_end, decode_end))


class AMD64DisassemblerDecoder(DisassemblyDecoder):
    def __init__(self):
        DisassemblyDecoder.__init__(self, capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64))

    def successors(self, i):
        if len(i.groups) > 0:
            groups = [i.group_name(g) for g in i.groups]
            if 'branch_relative' in groups:
                assert len(i.operands) == 1
                if i.op_str == 'jmp':
                    return [i.operands[0].imm]
                else:
                    return [i.operands[0].imm, i.address + i.size]
            elif 'jump' in groups:
                # how should an unknown successor be represented
                return [None]
        else:
            # true is intended to mean fall through
            return True


class AArch64DisassemblyDecoder(DisassemblyDecoder):
    def __init__(self):
        DisassemblyDecoder.__init__(self, capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM))

    def successors(self, i):
        if len(i.groups) > 0:
            groups = [i.group_name(g) for g in i.groups]
            if 'branch_relative' in groups:
                assert len(i.operands) == 1
                return i.operands[0].imm
            elif 'jump' in groups:
                return [None]
        else:
            return True


method_signature_re = re.compile(r'((?:\[*[VIJFDSCBZ])|(?:\[*L[^;]+;))')
primitive_types = {'I': 'int', 'J': 'long', 'V': 'void', 'F': 'float', 'D': 'double',
                   'S': 'short', 'C': 'char', 'B': 'byte', 'Z': 'boolean'}


class Method:
    """A Java Method decoded from a JVMTI assembly dump."""

    def __init__(self, class_signature, name, method_signature, source_file, line_number_table):
        self.line_number_table = line_number_table
        self.name = name
        args, return_type = method_signature[1:].split(')')
        arguments = re.findall(method_signature_re, args)
        self.method_arguments = '(' + ', '.join([Method.decode_type(x) for x in arguments]) + ')'
        self.return_type = Method.decode_type(return_type)
        self.source_file = source_file
        self.class_signature = Method.decode_class_signature(class_signature)

    def format_name(self, with_arguments=True):
        return self.class_signature + '.' + self.name + (self.method_arguments if with_arguments else '')

    def method_filter_format(self, with_arguments=False):
        return self.class_signature + '.' + self.name + (self.method_arguments if with_arguments else '')

    @staticmethod
    def decode_type(argument_type):
        result = argument_type
        arrays = ''
        while result[0] == '[':
            arrays = arrays + '[]'
            result = result[1:]
        if len(result) == 1:
            result = primitive_types[result]
        else:
            result = Method.decode_class_signature(result)
        return result + arrays

    @staticmethod
    def decode_class_signature(signature):
        if signature[0] == 'L' and signature[-1] == ';':
            return signature[1:-1].replace('/', '.')
        raise AssertionError('Bad signature: ' + signature)


class DebugFrame:
    def __init__(self, method, bci):
        self.method = method
        self.bci = bci

    def __str__(self):
        return '{}:{}'.format(self.method.format_name(with_arguments=False), self.bci)


class DebugInfo:
    def __init__(self, pc, frames):
        self.frames = frames
        self.pc = pc


class CompiledCodeInfo:
    """A generated chunk of HotSpot assembly, including any metadata"""

    def __init__(self, name, timestamp, code_addr, code_size,
                 code, generated, debug_info=None, methods=None):
        self.timestamp = timestamp
        self.code = code
        self.code_size = code_size
        self.code_addr = code_addr
        self.name = name
        self.debug_info = debug_info
        self.unload_time = None
        self.generated = generated
        self.events = []
        self.total_period = 0
        self.total_samples = 0
        self.methods = methods

    def __str__(self):
        return '0x{:x}-0x{:x} {} {}-{}'.format(self.code_begin(), self.code_end(), self.name, self.timestamp,
                                               self.unload_time or '')

    def set_unload_time(self, timestamp):
        self.unload_time = timestamp

    def code_begin(self):
        return self.code_addr

    def code_end(self):
        return self.code_addr + self.code_size

    def contains(self, pc, timestamp=None):
        if self.code_addr <= pc < self.code_end():
            # early stubs have a timestamp that is after their actual creation time
            # so treat any code which was never unloaded as persistent.
            return self.generated or timestamp is None or self.contains_timestamp(timestamp)
        return False

    def add(self, event):
        assert self.code_addr <= event.pc < self.code_end()
        self.events.append(event)
        self.total_period += event.period
        self.total_samples += event.samples

    def contains_timestamp(self, timestamp):
        return timestamp >= self.timestamp and \
               (self.unload_time is None or self.unload_time > timestamp)

    def get_annotations(self, pc):
        annotations = []
        prefix = None
        for event in self.events:
            if event.pc == pc:
                prefix = '{:5.2f}%'.format(100.0 * event.period / float(self.total_period))
                break
        if self.debug_info:
            for d in self.debug_info:
                if d.pc == pc:
                    for frame in d.frames:
                        annotations.append(str(frame))
                    break
        return annotations, prefix

    def disassemble(self, decoder, hot_only=False):
        print(self.name)
        print('0x{:x}-0x{:x} (samples={}, period={})'.format(self.code_begin(), self.code_end(),
                                                             self.total_samples, self.total_period))
        hotpc = set()
        for event in self.events:
            hotpc.add(event.pc)
        decoder.disassemble(self.code, self.code_addr, hotpc, show_regions=hot_only)
        print('')


class PerfEvent:
    """A simple wrapper around a single recorded even from the perf command"""

    def __init__(self, timestamp, events, period, pc, symbol, dso):
        self.dso = dso
        self.period = int(period)
        self.symbol = symbol
        self.pc = int(pc, 16)
        self.events = events
        self.timestamp = float(timestamp)
        self.samples = 1

    def __str__(self):
        return '{} {:x} {} {} {} {}'.format(self.timestamp, self.pc, self.events, self.period, self.symbol, self.dso)

    def symbol_name(self):
        if self.symbol == '[unknown]':
            return self.symbol + ' in ' + self.dso
        return self.symbol


class PerfOutput:
    """The decoded output of a perf record execution"""

    def __init__(self, files):
        self.events = []
        self.raw_events = []
        self.total_samples = 0
        self.total_period = 0
        self.top_methods = None
        with files.open_perf_output_file() as fp:
            self.read_perf_output(fp)

        self.merge_perf_events()

    @staticmethod
    def is_supported():
        return os.path.exists('/usr/bin/perf')

    @staticmethod
    def supports_dash_k_option():
        return subprocess.call(['perf', 'record', '-q', '-k', '1', 'echo'],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0

    @staticmethod
    def perf_convert_binary_command(files):
        return ['perf', 'script', '--fields', 'sym,time,event,dso,ip,sym,period', '-i',
                files.get_perf_binary_filename()]

    def read_perf_output(self, fp):
        """Parse the perf script output"""
        perf_re = re.compile(
            r'(?P<timestamp>[0-9]+\.[0-9]*):\s+(?P<period>[0-9]*)\s+(?P<events>[^\s]*):\s+'
            r'(?P<pc>[a-fA-F0-9]+)\s+(?P<symbol>.*)\s+\((?P<dso>.*)\)\s*')
        for line in fp.readlines():
            line = line.strip()
            m = perf_re.match(line)
            if m:
                event = PerfEvent(m.group('timestamp'), m.group('events'), m.group('period'),
                                  m.group('pc'), m.group('symbol'), m.group('dso'))
                self.events.append(event)
                self.total_period += event.period
            else:
                raise AssertionError('Unable to parse perf output: ' + line)
        self.total_samples = len(self.events)

    def merge_perf_events(self):
        """Collect repeated events at the same pc into a single PerfEvent."""
        self.raw_events = self.events
        events_by_address = {}
        for event in self.events:
            e = events_by_address.get(event.pc)
            if e:
                e.period = e.period + event.period
            else:
                # avoid mutating the underlying raw event
                events_by_address[event.pc] = copy.copy(event)
        self.events = events_by_address.values()

    def get_top_methods(self):
        """Get a list of symbols and event counts sorted by hottest first."""
        if not self.top_methods:
            hot_symbols = {}
            for event in self.events:
                key = (event.symbol, event.dso)
                count = hot_symbols.get(key)
                if count is None:
                    count = 0
                count = count + event.period
                hot_symbols[key] = count
            entries = [(s, d, c) for (s, d), c in hot_symbols.items()]

            def count_func(v):
                _, _, c = v
                return c

            entries.sort(key=count_func, reverse=True)
            self.top_methods = entries
        return self.top_methods


class GeneratedAssembly:
    """All the assembly generated by the HotSpot JIT including any helpers and the interpreter"""

    def __init__(self, files, verbose=False):
        self.code_info = []
        self.low_address = None
        self.high_address = None
        self.code_by_address = {}
        self.map = {}
        self.bucket_size = 8192
        with files.open_jvmti_asm_file() as fp:
            self.fp = fp
            tag = self.fp.read(8)
            if tag != filetag:
                raise AssertionError('Wrong magic number: Found {} but expected {}'.format(tag, filetag))
            self.major_version = self.read_jint()
            self.minor_version = self.read_jint()
            self.arch = self.read_string()
            self.timestamp = self.read_timestamp()
            self.java_nano_time = self.read_unsigned_jlong()
            self.read(fp, verbose)
            self.fp = None

    def decoder(self):
        if self.arch == 'amd64':
            return AMD64DisassemblerDecoder()
        if self.arch == 'aarch64':
            return AArch64DisassemblyDecoder()
        raise AssertionError('Unknown arch ' + self.arch)

    def round_up(self, value):
        return self.bucket_size * int((value + self.bucket_size - 1) / self.bucket_size)

    def round_down(self, value):
        return self.bucket_size * int(value / self.bucket_size)

    def build_search_map(self):
        for code in self.code_info:
            for pc in range(self.round_down(code.code_begin()), self.round_up(code.code_end()), self.bucket_size):
                entries = self.map.get(pc)
                if not entries:
                    entries = []
                    self.map[pc] = entries
                entries.append(code)

    def add(self, code_info):
        self.code_info.append(code_info)
        if not self.low_address:
            self.low_address = code_info.code_begin()
            self.high_address = code_info.code_end()
        else:
            self.low_address = min(self.low_address, code_info.code_begin())
            self.high_address = max(self.high_address, code_info.code_end())
        self.code_by_address[code_info.code_addr] = code_info

    def read(self, fp, verbose=False):
        while True:
            tag = self.read_jint()
            if not tag:
                return
            if tag == DynamicCodeTag:
                timestamp = self.read_timestamp()
                name = self.read_string()
                code_addr = self.read_unsigned_jlong()
                code_size = self.read_jint()
                code = fp.read(code_size)
                code_info = CompiledCodeInfo(name, timestamp, code_addr, code_size, code, True)
                self.add(code_info)
                if verbose:
                    print('Parsed {}'.format(code_info))
            elif tag == CompiledMethodUnloadTag:
                timestamp = self.read_timestamp()
                code_addr = self.read_unsigned_jlong()
                nmethod = self.code_by_address[code_addr]
                if not nmethod:
                    message = "missing code for {}".format(code_addr)
                    mx.abort(message)
                nmethod.set_unload_time(timestamp)
            elif tag == CompiledMethodLoadTag:
                timestamp = self.read_timestamp()
                code_addr = self.read_unsigned_jlong()
                code_size = self.read_jint()
                code = fp.read(code_size)
                tag = self.read_jint()
                if tag != MethodsTag:
                    mx.abort("Expected MethodsTag")
                methods_count = self.read_jint()
                methods = []
                for _ in range(methods_count):
                    class_signature = self.read_string()
                    method_name = self.read_string()
                    method_signature = self.read_string()
                    source_file = self.read_string()

                    line_number_table_count = self.read_jint()
                    line_number_table = []
                    for _ in range(line_number_table_count):
                        line_number_table.append((self.read_unsigned_jlong(), self.read_jint()))
                    method = Method(class_signature, method_name, method_signature, source_file, line_number_table)
                    methods.append(method)

                tag = self.read_jint()
                if tag != DebugInfoTag:
                    mx.abort("Expected DebugInfoTag")

                numpcs = self.read_jint()
                debug_infos = []
                for _ in range(numpcs):
                    pc = self.read_unsigned_jlong()
                    numstackframes = self.read_jint()
                    frames = []
                    for _ in range(numstackframes):
                        frames.append(DebugFrame(methods[self.read_jint()], self.read_jint()))
                    debug_infos.append(DebugInfo(pc, frames))
                nmethod = CompiledCodeInfo(methods[0].format_name(), timestamp, code_addr, code_size, code,
                                           False, debug_infos, methods)
                self.add(nmethod)
                if verbose:
                    print('Parsed {}'.format(nmethod))
            else:
                raise AssertionError("Unexpected tag {}".format(tag))

    def attribute_events(self, perf_data):
        assert self.low_address is not None and self.high_address is not None
        attributed = 0
        unknown = 0
        missing = 0
        for event in perf_data.events:
            if self.low_address <= event.pc < self.high_address:
                if self.add_event(event):
                    attributed += 1
                else:
                    missing += 1
            elif event.symbol == '[Unknown]':
                unknown += 1
        if missing > 50:
            # some versions of JVMTI leave out the stubs section of nmethod which occassionally gets ticks
            # so a small number of missing ticks should be ignored.
            mx.warn('{} events of {} could not be mapped to generated code'.format(missing, attributed + missing))

    def add_event(self, event):
        code_info = self.find(event.pc, event.timestamp)
        if code_info:
            code_info.add(event)
            if code_info.generated:
                event.dso = '[Generated]'
            else:
                event.dso = '[JIT]'
            event.symbol = code_info.name
            return True
        else:
            return False

    def search(self, pc):
        matches = []
        for code in self.code_info:
            if code.contains(pc):
                matches.append(code)
        return matches

    def get_stub_name(self, pc):
        """Map a pc to the name of a stub plus an offset."""
        for x in self.search(pc):
            if x.generated:
                offset = pc - x.code_addr
                if offset:
                    return '{}+0x{:x}'.format(x.name, offset)
                return x.name
        return None

    def find(self, pc, timestamp):
        if not self.map:
            self.build_search_map()
        index = self.round_down(pc)
        entries = self.map.get(index)
        if entries:
            entries = [x for x in entries if x.contains(pc)]
        if not entries:
            m = self.search(pc)
            if m:
                raise AssertionError(
                    'find has no hits for pc {:x} and timestamp {} but search found: {}'.format(pc, timestamp, str(
                        [str(x) for x in m])))
            return None

        # only a single PC match so don't bother checking the timestamp
        if len(entries) == 1:
            return entries[0]

        # check for an exact match first
        for x in entries:
            if x.contains(pc, timestamp):
                return x

        # events can occur before HotSpot has notified about the assembly so pick
        # the earliest method that was unloaded after the timestamp
        for x in entries:
            if x.unload_time is None or x.unload_time > timestamp:
                return x
        return None

    def print_all(self):
        for h in self.code_info:
            if h.name == 'Intepreter':
                continue
            decoder = self.decoder()

            def get_call_annotations(instruction):
                return h.get_annotations(instruction.address)

            def get_stub_call_name(instruction):
                if 'call' in instruction.groups():
                    return self.get_stub_name(instruction.insn.operands[0].imm)
                return None

            decoder.add_annotator(get_stub_call_name)
            decoder.add_annotator(get_call_annotations)

            h.disassemble(decoder)

    def read_jint(self):
        b = self.fp.read(4)
        if not b:
            return None
        assert len(b) == 4
        return int.from_bytes(b, byteorder='big', signed=True)

    def read_unsigned_jlong(self):
        b = self.fp.read(8)
        if not b:
            return None
        assert len(b) == 8
        return int.from_bytes(b, byteorder='big', signed=False)

    def read_string(self):
        length = self.read_jint()
        if length == -1:
            return None
        if length == 0:
            return ''
        body = self.fp.read(length)
        return body.decode('utf-8')

    def read_timestamp(self):
        sec = self.read_unsigned_jlong()
        nsec = self.read_unsigned_jlong()
        return sec + (nsec / 1000000000.0)

    def top_methods(self, include=None):
        entries = self.code_info
        if include:
            entries = [x for x in entries if include(x)]
        entries.sort(key=lambda x: x.total_period, reverse=True)
        return entries


def find_jvmti_asm_agent():
    """Find the path the JVMTI agent that records the disassembly"""
    d = mx.dependency('com.oracle.jvmtiasmagent')
    for source_file, _ in d.getArchivableResults(single=True):
        if not os.path.exists(source_file):
            mx.abort('{} hasn\'t been built yet'.format(source_file))
        return source_file
    return None


def profrecord_command(args):
    """Capture the profile of a Java program."""
    # capstone is not required for the capture step
    parser = ArgumentParser(description='Capture a profile of a Java program.')
    parser.add_argument('--script', help='Emit a script to run and capture annotated assembly', action='store_true')
    parser.add_argument('--experiment', '-E',
                        help='The directory containing the data files from the experiment',
                        action='store', required=True)
    parser.add_argument('--overwrite', '-O', help='Overwrite an existing dump directory',
                        action='store_true')
    parser.add_argument('--dump-hot', '-D', help='Run the program and then rerun it with dump options enabled for the hottest methods',
                        action='store_true')
    parser.add_argument('--dump-level', help='The Graal dump level to use with the --dump-hot option',
                        action='store', default=1)
    parser.add_argument('--limit', '-L', help='The number of hot methods to dump with the --dump-hot option',
                        action='store', default=5)
    options, args = parser.parse_known_args(args)
    files = FlatExperimentFiles.create(options.experiment, options.overwrite)

    if not PerfOutput.is_supported() and not options.script:
        mx.abort('Linux perf is unsupported on this platform')

    full_cmd = build_capture_command(files, args)
    convert_cmd = PerfOutput.perf_convert_binary_command(files)
    if options.script:
        print(mx.list_to_cmd_line(full_cmd))
        print('{} > {}'.format(mx.list_to_cmd_line(convert_cmd), files.get_perf_output_filename()))
    else:
        mx.run(full_cmd, nonZeroIsFatal=False)
        if not files.has_perf_binary():
            mx.abort('No perf binary file found')

        # convert the perf binary data into text format
        with files.open_perf_output_file(mode='w') as fp:
            mx.run(convert_cmd, out=fp)

        if options.dump_hot:
            assembly = GeneratedAssembly(files)
            perf = PerfOutput(files)
            assembly.attribute_events(perf)
            top = assembly.top_methods(include=lambda x: not x.generated and x.total_period > 0)[:options.limit]
            dump_path = files.create_dump_dir()
            method_filter = ','.join([x.methods[0].method_filter_format() for x in top])
            dump_arguments = ['-Dgraal.Dump=:{}'.format(options.dump_level),
                              '-Dgraal.MethodFilter=' + method_filter,
                              '-Dgraal.DumpPath=' + dump_path]

            # rerun the program with the new options capturing the dump in the experiment directory.
            # This overwrites the original profile information with a new profile that might be different
            # because of the effects of dumping.  This command might need to be smarter about the side effects
            # of dumping on the performance since the overhead of dumping might perturb the execution.  It's not
            # entirely clear how to cope with that though.
            full_cmd = build_capture_command(files, args, extra_vm_args=dump_arguments)
            convert_cmd = PerfOutput.perf_convert_binary_command(files)
            mx.run(full_cmd)
            with files.open_perf_output_file(mode='w') as fp:
                mx.run(convert_cmd, out=fp)


def profpackage_command(args):
    """Package a directory based profrecord experiment into a zip."""
    # capstone is not required for packaging
    parser = ArgumentParser(description='Capture a profile of a Java program.')
    parser.add_argument('--experiment', '-E',
                        help='The directory containing the data files from the experiment',
                        action='store', required=True)
    options, args = parser.parse_known_args(args)
    files = FlatExperimentFiles(directory=options.experiment)

    name = files.package()
    print('Created {}'.format(name))


def build_capture_args(files, extra_vm_args=None):
    jvmti_asm_file = files.get_jvmti_asm_filename()
    perf_binary_file = files.get_perf_binary_filename()
    perf_cmd = ['perf', 'record']
    if not PerfOutput.is_supported() or PerfOutput.supports_dash_k_option():
        perf_cmd += ['-k', '1']
    perf_cmd += ['--freq', '1000', '--event', 'cycles', '--output', perf_binary_file]
    vm_args = ['-agentpath:{}={}'.format(find_jvmti_asm_agent(), jvmti_asm_file), '-XX:+UnlockDiagnosticVMOptions',
               '-XX:+DebugNonSafepoints']
    if extra_vm_args:
        vm_args += extra_vm_args
    return perf_cmd, vm_args


def build_capture_command(files, command_line, extra_vm_args=None):
    java_cmd = command_line[0]
    java_args = command_line[1:]
    perf_cmd, vm_args = build_capture_args(files, extra_vm_args)
    full_cmd = perf_cmd + [java_cmd] + vm_args + java_args
    return full_cmd


def profhot_command(args):
    """Display the top hot methods and their annotated disassembly"""
    check_capstone_import('profhot')
    parser = ArgumentParser(description='')
    parser.add_argument('--experiment', '-E',
                        help='The directory containing the data files from the experiment',
                        action='store', required=True)
    parser.add_argument('--limit', '-n', help='Show the top n entries', action='store', default=10, type=int)
    options = parser.parse_args(args)
    files = ExperimentFiles.open(options)
    perf_data = PerfOutput(files)
    assembly = GeneratedAssembly(files)
    assembly.attribute_events(perf_data)
    entries = perf_data.get_top_methods()
    non_jit_entries = [(s, d, c) for s, d, c in entries if d not in ('[JIT]', '[Generated]')]
    print('Hot C functions:')
    for symbol, _, count in non_jit_entries[:options.limit]:
        print('{:8.2f}% {}'.format(100 * (float(count) / perf_data.total_period), symbol))
    print('')

    hot = assembly.top_methods(lambda x: x.total_period > 0)
    hot = hot[:options.limit]
    print('Hot generated code:')
    for code in hot:
        print('{:8.2f}% {}'.format(100 * (float(code.total_period) / perf_data.total_period), code.name))
    print('')

    for h in hot:
        if h.name == 'Interpreter':
            continue
        decoder = assembly.decoder()

        def get_call_annotations(instruction):
            return h.get_annotations(instruction.address)

        def get_stub_call_name(instruction):
            if 'call' in instruction.groups():
                return assembly.get_stub_name(instruction.insn.operands[0].imm)
            return None

        decoder.add_annotator(get_stub_call_name)
        decoder.add_annotator(get_call_annotations)

        h.disassemble(decoder, hot_only=True)


def profasm_command(args):
    """Dump the assembly from a jvmtiasmagent dump"""
    check_capstone_import('profasm')
    parser = ArgumentParser(description='')
    parser.add_argument('--experiment', '-E',
                        help='The directory or zip containing the data files from the experiment',
                        action='store', required=True)
    options = parser.parse_args(args)
    files = ExperimentFiles.open(options)
    assembly = GeneratedAssembly(files)
    assembly.print_all()