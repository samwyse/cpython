"The actual converter class"

import opcode

from rattlesnake.blocks import Block
from rattlesnake.instructions import *
from rattlesnake.util import enumerate_reversed, LineNumberDict

class OptimizeFilter:
    """Base peephole optimizer class for Python byte code.

Instances of OptimizeFilter subclasses are chained together in a
pipeline, each one responsible for a single optimization."""

    NOP_OPCODE = opcode.opmap['NOP']
    EXT_ARG_OPCODE = opcode.opmap["EXTENDED_ARG"]

    def __init__(self, codeobj):
        """input must be a code object."""
        self.codeobj = codeobj
        self.codestr = codeobj.co_code
        self.varnames = codeobj.co_varnames
        self.names = codeobj.co_names
        self.constants = codeobj.co_consts
        self.nlocals = codeobj.co_nlocals
        self.stacksize = codeobj.co_stacksize
        self.blocks = {
            "PyVM": [],
            "RVM": [],
        }

    def findlabels(self, code):
        "Find target addresses in the code."
        labels = {0}
        n = len(code)
        carry_oparg = 0
        for i in range(0, n, 2):
            op, oparg = code[i:i+2]
            carry_oparg = carry_oparg << 8 | oparg
            if op == self.EXT_ARG_OPCODE:
                continue
            oparg, carry_oparg = carry_oparg, 0
            if op in opcode.hasjrel:
                # relative jump
                labels.add(i + oparg)
                #print(i, "labels:", labels)
            elif op in opcode.hasjabs:
                # abs jump
                labels.add(oparg)
                #print(i, "labels:", labels)
        labels = sorted(labels)
        return labels

    def convert_jump_targets_to_blocks(self):
        "Convert jump target addresses to block numbers in PyVM blocks."
        blocks = self.blocks["PyVM"]
        assert blocks[0].block_type == "PyVM"
        for block in blocks:
            for instr in block:
                if instr.is_jump():
                    for tblock in blocks:
                        if instr.target_address == tblock.address:
                            instr.target = tblock.block_number
                            break
                    assert instr.target != -1
                    # No longer required.
                    del instr.target_address

    def find_blocks(self):
        """Convert code byte string to block form.

        JUMP instruction targets are converted to block numbers at the end.

        """
        blocks = self.blocks["PyVM"]
        labels = self.findlabels(self.codestr)
        line_numbers = LineNumberDict(self.codeobj)
        #print(">>> labels:", labels)
        n = len(self.codestr)
        block_num = 0
        ext_oparg = 0
        for offset in range(0, n, 2):
            if offset in labels:
                block = Block("PyVM", self, block_num)
                block.address = offset
                #print(">>> new block:", block, "@", offset)
                block_num += 1
                blocks.append(block)
            (op, oparg) = self.codestr[offset:offset+2]
            #print(">>>", op, opcode.opname[op], oparg)
            # Elide EXTENDED_ARG opcodes, constructing the effective
            # oparg as we go.
            if op == self.EXT_ARG_OPCODE:
                ext_oparg = ext_oparg << 8 | oparg
            else:
                oparg = ext_oparg << 8 | oparg
                instr = PyVMInstruction(op, block, opargs=(oparg,))
                if instr.is_jump():
                    address = oparg
                    if instr.is_rel_jump():
                        # Convert to absolute
                        address += offset
                    #print(f">> {block.block_number} found a JUMP"
                    #      f" @ {offset} target_addr={address}")
                    instr = JumpInstruction(op, block, address=address)
                instr.line_number = line_numbers[offset]
                #print(">>>", instr)
                block.append(instr)
                ext_oparg = 0
        self.convert_jump_targets_to_blocks()

class InstructionSetConverter(OptimizeFilter):
    """convert stack-based VM code into register-oriented VM code.

    this class consists of a series of small methods, each of which knows
    how to convert a small number of stack-based instructions to their
    register-based equivalents.  A dispatch table in optimize_block keyed
    by the stack-based instructions selects the appropriate routine.
    """

    dispatch = {}

    def __init__(self, code):
        # input to this guy is a code object
        super().__init__(code)
        # Stack starts right after locals. Together, the locals and
        # the space allocated for the stack form a single register
        # file.
        self.stacklevel = self.nlocals
        self.max_stacklevel = self.nlocals + self.stacksize

        # print(">> nlocals:", self.nlocals)
        # print(">> stacksize:", self.stacksize)
        # print(">> starting stacklevel:", self.stacklevel)
        # print(">> max stacklevel:", self.max_stacklevel)
        assert self.max_stacklevel <= 127, "locals+stack are too big!"

    def set_block_stacklevel(self, target, level):
        """set the input stack level for particular block"""
        #print(">> set_block_stacklevel:", (target, level))
        self.blocks["RVM"][target].set_stacklevel(level)

    # series of operations below mimic the stack changes of various
    # stack operations so we know what slot to find particular values in
    def push(self):
        """increment and return next writable slot on the stack"""
        self.stacklevel += 1
        #print(">> push:", self.stacklevel)
        # This gets a bit tricky.  In response to an issue I opened,
        # bpo40315, Serhiy Storchaka wrote:

        # > [U]nreachable code is left because some code (in
        # > particularly the lineno setter of the frame object)
        # > depends on instructions which may be in the unreachable
        # > code to determine the boundaries of programming blocks. It
        # > is safer to keep some unreachable code.

        # > You can just ignore the code which uses the stack past
        # > co_stacksize.

        # So, I think we can raise a special exception (AssertionError
        # seems a bit general) here, and in those convert functions
        # where the stack only grows (mostly LOADs, but eventually
        # IMPORTs as well), we can catch and ignore it.
        if self.stacklevel > self.max_stacklevel:
            raise StackSizeException(
                f"Overran the allocated stack/register space!"
                f" {self.stacklevel} > {self.max_stacklevel}"
            )
        return self.stacklevel - 1

    def pop(self):
        """return top readable slot on the stack and decrement"""
        self.stacklevel -= 1
        #print(">> pop:", self.stacklevel)
        if self.stacklevel < self.nlocals:
            raise StackSizeException(
                f"Stack slammed into locals!"
                f" {self.stacklevel} < {self.nlocals}"
            )
        return self.stacklevel

    def peek(self, n):
        """return n'th readable slot in the stack without decrement."""
        if self.stacklevel - n < self.nlocals:
            raise StackSizeException(
                f"Peek read past bottom of locals!"
                f" {self.stacklevel - n} < {self.nlocals}"
            )
        return self.stacklevel - n

    def top(self):
        """return top readable slot on the stack"""
        #print(">> top:", self.stacklevel)
        return self.stacklevel

    def gen_rvm(self):
        self.find_blocks()
        self.blocks["RVM"] = []
        for pyvm_block in self.blocks["PyVM"]:
            rvm_block = Block("RVM", self, block_number=pyvm_block.block_number)
            self.blocks["RVM"].append(rvm_block)
        for (rvm, pyvm) in zip(self.blocks["RVM"], self.blocks["PyVM"]):
            try:
                pyvm.gen_rvm(rvm)
            except KeyError:
                self.display_blocks(self.blocks["PyVM"])
                raise

    # A small, detailed example forward propagating the result of a
    # fast load and backward propagating the result of a fast
    # store. (Using abbreviated names: LFR == LOAD_FAST_REG, etc.)

    #                       Forward    Reverse     Action
    # LFR, (2, 1)           %r2 -> %r1             NOP
    # LCR, (3, 1)               |
    # BMR, (2, 3, 2)            v          ^       src2 = %r1, dst = %r0
    # SFR, (0, 2)                      %r2 -> %r0  NOP

    # Apply actions:

    # NOP
    # LCR, (3, 1)
    # BMR, (0, 3, 1)
    # NOP

    # Delete NOPs:

    # LCR, (3, 1)
    # BMR, (0, 3, 1)

    # Result:

    # * 10 bytes in code string instead of 18

    # * Two operations, three EXT_ARG instead of four operations, five
    #   EXT_ARG

    # * One load instead of two

    # * No explicit stores

    # Consider forward propagation operating on a few
    # instructions. Before:

    #  0 Instruction(LOAD_FAST_REG, (2, 0)) (4)
    #  4 Instruction(LOAD_FAST_REG, (3, 1)) (4)
    #  8 Instruction(COMPARE_OP_REG, (2, 2, 3, 4)) (8)
    # 16 Instruction(JUMP_IF_FALSE_REG, (1, 2)) (4)
    # 20 Instruction(LOAD_FAST_REG, (2, 1)) (4)
    # 24 Instruction(LOAD_CONST_REG, (3, 1)) (4)
    # 28 Instruction(BINARY_SUBTRACT_REG, (2, 2, 3)) (6)

    # Immediately after:

    #  0 Instruction(NOP, (0,)) (2)
    #  2 Instruction(NOP, (0,)) (2)
    #  4 Instruction(COMPARE_OP_REG, (2, 0, 1, 4)) (8)
    # 12 Instruction(JUMP_IF_FALSE_REG, (1, 2)) (4)
    # 16 Instruction(NOP, (0,)) (2)
    # 18 Instruction(LOAD_CONST_REG, (3, 1)) (4)
    # 22 Instruction(BINARY_SUBTRACT_REG, (2, 1, 1)) (6)

    # When the first two LFR instructions were added to the block, we
    # push()'d, indicating that registers 2 and 3 were occupied.  When
    # forward propagation replaces them with NOPs, I think we need to
    # at least note that they are now free. pop() might not be the
    # right thing to do, as we have already added a LCR instruction
    # whose destination was register 3.  Maybe we maintain a register
    # free list?  Or maybe it doesn't matter.  We just don't use as
    # many registers.  Perhaps when someone implements a better
    # register allocation scheme the registers which were freed up in
    # this stage will be useful.

    def forward_propagate_fast_loads(self):
        "LOAD_FAST_REG should be a NOP..."
        self.mark_protected_loads()
        prop_dict = {}
        dirty = None
        for block in self.blocks["RVM"]:
            for (i, instr) in enumerate(block):
                if (isinstance(instr, LoadFastInstruction) and
                    instr.name == "LOAD_FAST_REG" and
                    not instr.protected):
                    # Will map future references to the load's
                    # destination register to its source.
                    prop_dict[instr.dest] = instr.source1
                    # The load is no longer needed, so replace it with
                    # a NOP.
                    block[i] = NOPInstruction(self.NOP_OPCODE, block)
                    if dirty is None:
                        dirty = block.block_number
                else:
                    for srckey in ("source1", "source2"):
                        src = getattr(instr, srckey, None)
                        if src is not None:
                            setattr(instr, srckey, prop_dict.get(src, src))
                    dst = getattr(instr, "dest", None)
                    if dst is not None:
                        # If the destination register is overwritten,
                        # remove it from the dictionary as it's no
                        # longer valid.
                        try:
                            del prop_dict[dst]
                        except KeyError:
                            pass
        self.mark_dirty(dirty)

    def backward_propagate_fast_stores(self):
        "STORE_FAST_REG should be a NOP..."
        # This is similar to forward_propagate_fast_loads, but we work
        # from back to front through the block list, map src to dst in
        # STORE instructions and update source registers until we see
        # a register appear as a source in an earlier instruction.
        prop_dict = {}
        dirty = None
        for block in self.blocks["RVM"]:
            for (i, instr) in enumerate_reversed(block):
                if isinstance(instr, StoreFastInstruction):
                    # Will map earlier references to the store's
                    # source registers to its destination.
                    prop_dict[instr.source1] = instr.dest
                    # Elide...
                    block[i] = NOPInstruction(self.NOP_OPCODE, block)
                    if dirty is None:
                        dirty = block.block_number
                    else:
                        dirty = min(block.block_number, dirty)
                else:
                    dst = getattr(instr, "dest", None)
                    if dst is not None:
                        # If the destination register can be mapped to
                        # a source, replace it here.
                        instr.dest = prop_dict.get(dst, dst)
                    for srckey in ("source1", "source2"):
                        src = getattr(instr, srckey, None)
                        try:
                            del prop_dict[src]
                        except KeyError:
                            pass
        self.mark_dirty(dirty)

    def mark_protected_loads(self):
        "Identify LoadFastInstructions which must not be removed."
        # Sometimes registers are used implicitly, so LOADs into them
        # can't be removed so easily.  Consider the code necessary to
        # construct this list:
        #
        # [1, x, y]
        #
        # Basic RVM code looks like this (ignoring EXT_ARG instructions):
        #
        # LOAD_CONST_REG            769 (%r3 <- 1)
        # LOAD_FAST_REG            1024 (%r4 <- %r0)
        # LOAD_FAST_REG            1281 (%r5 <- %r1)
        # BUILD_LIST_REG         131843 (0, 2, 3, 3)
        #
        # The BUILD_LIST_REG instruction requires its inputs be in
        # registers %r3 through %r5.  Accordingly, the two LOAD_FAST_REG
        # instructions used to (partially) construct its inputs must be
        # preserved.  BUILD_TUPLE_REG and CALL_FUNCTION_REG will have
        # similar implicit references.  Other instructions might as well.
        # One way to deal with this might be to identify such implicit
        # uses and mark the corresponding LOADs as "protected."  Then
        # execute the normal forward propagation code, skipping over those
        # LOADs.
        #
        # This example is more challenging:
        #
        # def _tuple(a):
        #     return (a, a+2, a+3, a+4)
        #
        # Note that the LOAD_FAST_REG of the initial 'a' won't
        # immediately precede the BUILD_TUPLE_REG instruction. The
        # various expression evaluations separate them.  Instead of
        # just looking at the immediately preceding instr.length
        # instructions and marking LOAD_FAST_REG as protected, we need
        # to look at the last writes to all registers between
        # instr.dest and instr.dest+instr.length
        #
        # I've hacked something together here.  Not sure it's entirely
        # correct (it's certainly still incomplete, failing to
        # consider calls at this point), but the failing test passes,
        # so we're done. :-)
        for block in self.blocks["RVM"]:
            for (i, instr) in enumerate(block):
                if isinstance(instr, BuildSeqInstruction):
                    first = instr.dest
                    last = first + instr.length
                elif isinstance(instr, CallInstruction):
                    first = instr.dest
                    last = first + instr.nargs
                else:
                    # Maybe others not yet handled?
                    continue
                saved = {}
                reg = first
                while reg < last:
                    saved[reg] = False
                    reg += 1

                # Look backward to find writes to the registers in
                # the saved dict.
                for index in range(i - 1, -1, -1):
                    reg = getattr(block[index], "dest", None)
                    if reg not in saved:
                        # No mention of any of our registers.
                        continue
                    if saved[reg]:
                        # This operation is earlier than the
                        # latest write to reg, so it's okay to
                        # elide it.
                        continue
                    if hasattr(block[index], "protected"):
                        # One of our registers is mentioned in a
                        # LOAD, so protect it and mark the
                        # register as saved.
                        block[index].protected = True
                        saved[reg] = True
                    if all(saved.values()):
                        # We've protected every LOAD into one of
                        # our registers, so we're done
                        break
                else:
                    # We got here without marking every register
                    # of interest saved.  That's okay, as not
                    # everything which affects our input registers
                    # will be a LOAD.
                    pass

    def delete_nops(self):
        "NOP instructions can safely be removed."
        dirty = None
        for block in self.blocks["RVM"]:
            for (i, instr) in enumerate_reversed(block):
                if isinstance(instr, NOPInstruction):
                    del block[i]
                    if dirty is None:
                        dirty = block.block_number
                    else:
                        dirty = min(block.block_number, dirty)
        self.mark_dirty(dirty)

    def mark_dirty(self, dirty):
        "Reset addresses on dirty blocks."
        # Every block downstream from the first modified block is
        # dirty.
        if dirty is None:
            return
        for block in self.blocks["RVM"][dirty:]:
            #print("??? mark block", block.block_number, "dirty")
            block.address = -1
        # Except the address of the first block is always known.
        # pylint: disable=protected-access
        self.blocks["RVM"][0]._address = 0

    def display_blocks(self, blocks):
        "debug"
        print("globals:", self.names)
        print("locals:", self.varnames)
        print("constants:", self.constants)
        print("code len:", sum(block.codelen() for block in blocks))
        print("first lineno:", self.codeobj.co_firstlineno)
        for block in blocks:
            print(block)
            block.display()
        print()

    def unary_convert(self, instr, block):
        opname = "%s_REG" % opcode.opname[instr.opcode]
        src = self.pop()
        dst = self.push()
        return UnaryOpInstruction(opcode.opmap[opname], block,
                                  dest=dst, source1=src)
    dispatch[opcode.opmap['UNARY_INVERT']] = unary_convert
    dispatch[opcode.opmap['UNARY_POSITIVE']] = unary_convert
    dispatch[opcode.opmap['UNARY_NEGATIVE']] = unary_convert
    dispatch[opcode.opmap['UNARY_NOT']] = unary_convert

    def binary_convert(self, instr, block):
        opname = "%s_REG" % opcode.opname[instr.opcode]
        ## TBD... Still not certain I have argument order/byte packing correct.
        # dst <- src1 OP src2
        src2 = self.pop()       # right-hand register src
        src1 = self.pop()       # left-hand register src
        dst = self.push()       # dst
        return BinOpInstruction(opcode.opmap[opname], block,
                                dest=dst, source1=src1, source2=src2)
    dispatch[opcode.opmap['BINARY_POWER']] = binary_convert
    dispatch[opcode.opmap['BINARY_MULTIPLY']] = binary_convert
    dispatch[opcode.opmap['BINARY_MATRIX_MULTIPLY']] = binary_convert
    dispatch[opcode.opmap['BINARY_TRUE_DIVIDE']] = binary_convert
    dispatch[opcode.opmap['BINARY_FLOOR_DIVIDE']] = binary_convert
    dispatch[opcode.opmap['BINARY_MODULO']] = binary_convert
    dispatch[opcode.opmap['BINARY_ADD']] = binary_convert
    dispatch[opcode.opmap['BINARY_SUBTRACT']] = binary_convert
    dispatch[opcode.opmap['BINARY_LSHIFT']] = binary_convert
    dispatch[opcode.opmap['BINARY_RSHIFT']] = binary_convert
    dispatch[opcode.opmap['BINARY_AND']] = binary_convert
    dispatch[opcode.opmap['BINARY_XOR']] = binary_convert
    dispatch[opcode.opmap['BINARY_OR']] = binary_convert
    dispatch[opcode.opmap['BINARY_SUBSCR']] = binary_convert
    dispatch[opcode.opmap['INPLACE_POWER']] = binary_convert
    dispatch[opcode.opmap['INPLACE_MULTIPLY']] = binary_convert
    dispatch[opcode.opmap['INPLACE_MATRIX_MULTIPLY']] = binary_convert
    dispatch[opcode.opmap['INPLACE_TRUE_DIVIDE']] = binary_convert
    dispatch[opcode.opmap['INPLACE_FLOOR_DIVIDE']] = binary_convert
    dispatch[opcode.opmap['INPLACE_MODULO']] = binary_convert
    dispatch[opcode.opmap['INPLACE_ADD']] = binary_convert
    dispatch[opcode.opmap['INPLACE_SUBTRACT']] = binary_convert
    dispatch[opcode.opmap['INPLACE_LSHIFT']] = binary_convert
    dispatch[opcode.opmap['INPLACE_RSHIFT']] = binary_convert
    dispatch[opcode.opmap['INPLACE_AND']] = binary_convert
    dispatch[opcode.opmap['INPLACE_XOR']] = binary_convert
    dispatch[opcode.opmap['INPLACE_OR']] = binary_convert

    # def subscript_convert(self, instr, block):
    #     op = instr.opcode
    #     if op == opcode.opmap['STORE_SUBSCR']:
    #         index = self.pop()
    #         obj = self.pop()
    #         val = self.pop()
    #         return Instruction(opcode.opmap['STORE_SUBSCR_REG'],
    #                            (obj, index, val))
    #     if op == opcode.opmap['DELETE_SUBSCR']:
    #         index = self.pop()
    #         obj = self.pop()
    #         return Instruction(opcode.opmap['DELETE_SUBSCR_REG'],
    #                            (obj, index))
    #     raise ValueError(f"Unhandled opcode {opcode.opname[op]}")
    # dispatch[opcode.opmap['STORE_SUBSCR']] = subscript_convert
    # dispatch[opcode.opmap['DELETE_SUBSCR']] = subscript_convert

    def function_convert(self, instr, block):
        op = instr.opcode
        oparg = instr.opargs[0] # All PyVM opcodes have a single oparg
        if op == opcode.opmap['CALL_FUNCTION']:
            nargs = oparg
            dest = self.top() - nargs - 1
            for _ in range(nargs):
                _x = self.pop()
            return CallInstruction(opcode.opmap['CALL_FUNCTION_REG'],
                                   block, nargs=nargs, dest=dest)
        if op == opcode.opmap['CALL_FUNCTION_KW']:
            nargs = oparg
            nreg = self.top() - 1
            dest = self.top() - nargs - 2
            #print(nargs, nreg, dest)
            for _ in range(nargs + 1):
                _x = self.pop()
            return CallInstructionKW(opcode.opmap['CALL_FUNCTION_KW_REG'],
                                     block, nargs=nargs, nreg=nreg, dest=dest)
    dispatch[opcode.opmap['CALL_FUNCTION']] = function_convert
    dispatch[opcode.opmap['CALL_FUNCTION_KW']] = function_convert

    def jump_convert(self, instr, block):
        op = instr.opcode
        oparg = instr.opargs[0] # All PyVM opcodes have a single oparg
        if op == opcode.opmap['RETURN_VALUE']:
            opname = f"{opcode.opname[op]}_REG"
            return ReturnInstruction(opcode.opmap[opname], block,
                                     source1=self.pop())
        if op in (opcode.opmap['POP_JUMP_IF_FALSE'],
                    opcode.opmap['POP_JUMP_IF_TRUE']):
            opname = f"{opcode.opname[op]}_REG"[4:]
            self.set_block_stacklevel(oparg, self.top())
            return JumpIfInstruction(opcode.opmap[opname], block,
                                     target=instr.target, source1=self.pop())
        if op in (opcode.opmap['JUMP_FORWARD'],
                    opcode.opmap['JUMP_ABSOLUTE']):
            # Reused unchanged from PyVM
            opname = f"{opcode.opname[op]}"
            return JumpAbsInstruction(opcode.opmap[opname], block,
                                      target=instr.target)
    dispatch[opcode.opmap['JUMP_FORWARD']] = jump_convert
    dispatch[opcode.opmap['JUMP_ABSOLUTE']] = jump_convert
    dispatch[opcode.opmap['POP_JUMP_IF_FALSE']] = jump_convert
    dispatch[opcode.opmap['POP_JUMP_IF_TRUE']] = jump_convert
    dispatch[opcode.opmap['JUMP_ABSOLUTE']] = jump_convert
    dispatch[opcode.opmap['RETURN_VALUE']] = jump_convert

    def load_convert(self, instr, block):
        op = instr.opcode
        oparg = instr.opargs[0] # All PyVM opcodes have a single oparg
        src = oparg         # offset into localsplus
        try:
            dst = self.push()
        except StackSizeException:
            # unreachable code - stop translating
            return None
        opname = f"{opcode.opname[op]}_REG"

        if op == opcode.opmap['LOAD_FAST']:
            instr = LoadFastInstruction(opcode.opmap[opname], block,
                                        dest=dst, source1=src)
        elif op == opcode.opmap['LOAD_CONST']:
            instr = LoadConstInstruction(opcode.opmap[opname], block,
                                         dest=dst, name1=src)
        elif op == opcode.opmap['LOAD_GLOBAL']:
            instr = LoadGlobalInstruction(opcode.opmap[opname], block,
                                          dest=dst, name1=src)
        return instr
    dispatch[opcode.opmap['LOAD_CONST']] = load_convert
    dispatch[opcode.opmap['LOAD_GLOBAL']] = load_convert
    dispatch[opcode.opmap['LOAD_FAST']] = load_convert

    def store_convert(self, instr, block):
        op = instr.opcode
        oparg = instr.opargs[0] # All PyVM opcodes have a single oparg
        opname = f"{opcode.opname[op]}_REG"
        if op == opcode.opmap['STORE_FAST']:
            dst = oparg
            src = self.pop()
            return StoreFastInstruction(opcode.opmap[opname], block,
                                        dest=dst, source1=src)
        elif op == opcode.opmap['STORE_GLOBAL']:
            name1 = oparg
            src = self.pop()
            return StoreGlobalInstruction(opcode.opmap[opname], block,
                                          name1=name1, source1=src)
    dispatch[opcode.opmap['STORE_FAST']] = store_convert
    dispatch[opcode.opmap['STORE_GLOBAL']] = store_convert

    # def attr_convert(self, instr, block):
    #     op = instr.opcode
    #     oparg = instr.opargs[0] # All PyVM opcodes have a single oparg
    #     if op == opcode.opmap['LOAD_ATTR']:
    #         obj = self.pop()
    #         attr = oparg
    #         dst = self.push()
    #         return Instruction(opcode.opmap['LOAD_ATTR_REG'], block,
    #                            (dst, obj, attr))
    #     if op == opcode.opmap['STORE_ATTR']:
    #         obj = self.pop()
    #         attr = oparg
    #         val = self.pop()
    #         return Instruction(opcode.opmap['STORE_ATTR_REG'], block,
    #                            (obj, attr, val))
    #     if op == opcode.opmap['DELETE_ATTR']:
    #         obj = self.pop()
    #         attr = oparg
    #         return Instruction(opcode.opmap['DELETE_ATTR_REG'], block,
    #                            (obj, attr))
    #     raise ValueError(f"Unhandled opcode {opcode.opname[op]}")
    # dispatch[opcode.opmap['STORE_ATTR']] = attr_convert
    # dispatch[opcode.opmap['DELETE_ATTR']] = attr_convert
    # dispatch[opcode.opmap['LOAD_ATTR']] = attr_convert

    def seq_convert(self, instr, block):
        op = instr.opcode
        oparg = instr.opargs[0] # All PyVM opcodes have a single oparg
        opname = "%s_REG" % opcode.opname[op]
        build_map = opcode.opmap['BUILD_MAP']
        if op in (opcode.opmap['BUILD_LIST'],
                  opcode.opmap['BUILD_TUPLE'],
                  build_map):
            eltlen = 2 if op == build_map else 1
            n = oparg
            for _ in range(n * eltlen):
                self.pop()
            dst = self.push()
            #print(f">>> dst: {dst}, len: {n}")
            return BuildSeqInstruction(opcode.opmap[opname], block,
                                       length=n, dest=dst)
        if op == opcode.opmap['LIST_EXTEND']:
            src = self.pop()
            dst = self.peek(oparg)
            return ExtendSeqInstruction(opcode.opmap[opname], block,
                                        source1=src, dest=dst)

        # if op == opcode.opmap['UNPACK_SEQUENCE']:
        #     n = oparg
        #     src = self.pop()
        #     for _ in range(n):
        #         self.push()
        #     return Instruction(opcode.opmap[opname], block)
    dispatch[opcode.opmap['BUILD_TUPLE']] = seq_convert
    dispatch[opcode.opmap['BUILD_LIST']] = seq_convert
    dispatch[opcode.opmap['LIST_EXTEND']] = seq_convert
    dispatch[opcode.opmap['BUILD_MAP']] = seq_convert
    # dispatch[opcode.opmap['UNPACK_SEQUENCE']] = seq_convert

    def compare_convert(self, instr, block):
        op = instr.opcode
        oparg = instr.opargs[0] # All PyVM opcodes have a single oparg
        if op == opcode.opmap['COMPARE_OP']:
            cmpop = oparg
            src2 = self.pop()
            src1 = self.pop()
            dst = self.push()
            return CompareOpInstruction(opcode.opmap['COMPARE_OP_REG'],
                                        block,
                                        dest=dst, source1=src1,
                                        source2=src2, compare_op=cmpop)
    dispatch[opcode.opmap['COMPARE_OP']] = compare_convert

    # def stack_convert(self, instr, block):
    #     op = instr.opcode
    #     if op == opcode.opmap['POP_TOP']:
    #         self.pop()
    #         return NOPInstruction(self.NOP_OPCODE, block)
    #     if op == opcode.opmap['DUP_TOP']:
    #         # nop
    #         _dummy = self.top()
    #         _dummy = self.push()
    #         return NOPInstruction(self.NOP_OPCODE, block)
    #     if op == opcode.opmap['ROT_TWO']:
    #         return Instruction(opcode.opmap['ROT_TWO_REG'], block,
    #                            (self.top(),))
    #     if op == opcode.opmap['ROT_THREE']:
    #         return Instruction(opcode.opmap['ROT_THREE_REG'], block,
    #                            (self.top(),))
    #     if op == opcode.opmap['POP_BLOCK']:
    #         return Instruction(opcode.opmap['POP_BLOCK_REG'], block)
    #     raise ValueError(f"Unhandled opcode {opcode.opname[op]}")
    # dispatch[opcode.opmap['POP_TOP']] = stack_convert
    # dispatch[opcode.opmap['ROT_TWO']] = stack_convert
    # dispatch[opcode.opmap['ROT_THREE']] = stack_convert
    # dispatch[opcode.opmap['DUP_TOP']] = stack_convert
    # dispatch[opcode.opmap['POP_BLOCK']] = stack_convert

    def misc_convert(self, instr, block):
        op = instr.opcode
        # if op == opcode.opmap['IMPORT_NAME']:
        #     dst = self.push()
        #     return Instruction(opcode.opmap['IMPORT_NAME_REG'], block,
        #                        (dst, oparg[0]))
        # opname = "%s_REG" % opcode.opname[op]
        # if op == opcode.opmap['PRINT_EXPR']:
        #     src = self.pop()
        #     return Instruction(opcode.opmap[opname], block, (src,))
    #dispatch[opcode.opmap['IMPORT_NAME']] = misc_convert
    #dispatch[opcode.opmap['PRINT_EXPR']] = misc_convert

    def __bytes__(self):
        "Return generated byte string."
        instr_bytes = []
        for block in self.blocks["RVM"]:
            instr_bytes.append(bytes(block))
        return b"".join(instr_bytes)

    def get_lnotab(self):
        firstlineno = self.codeobj.co_firstlineno
        last_line_number = firstlineno
        last_address = 0
        address = 0
        lnotab = []
        for block in self.blocks["RVM"]:
            for instr in block.instructions:
                line_number = instr.line_number
                if line_number > last_line_number:
                    offset = line_number - last_line_number
                    lnotab.append(address - last_address)
                    lnotab.append(offset)
                    last_line_number = line_number
                    last_address = address
                address += len(instr)
        return bytes(lnotab)

class StackSizeException(Exception):
    """Raised when the stack would grow too small or too large.

    See bpo40315.
    """
