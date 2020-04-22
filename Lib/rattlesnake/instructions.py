"""Individual instructions.

Each Instruction object has an opcode (a fixed integer) and both name
and opargs attributes which are implemented as properties. They
reference back to the block where they are defined (again, a fixed
attribute). In addition, various Instruction subclasses may implement
other attributes needed for specialized tasks. For example, jump
instructions need to calculate addresses (relative or absolute) which
will depend on their enclosing block's address.

"""

import opcode

from rattlesnake.util import decode_oparg

class Instruction:
    """Represent an instruction in either PyVM or RVM.

    Instruction opargs are currently represented by a tuple. Its
    makeup varies by Instruction subclass.

    """

    EXT_ARG_OPCODE = opcode.opmap["EXTENDED_ARG"]

    def __init__(self, op, block, **kwargs):
        self.opcode = op
        self._opargs = (0,)
        self.block = block
        # Index into parent block's instructions list.
        self.index = -1
        # unset (or same as previous instruction?)
        self.line_number = -1
        if kwargs:
            raise ValueError(f"Non-empty kwargs at top level {kwargs}")

    @property
    def name(self):
        "Human-readable name for the opcode."
        return opcode.opname[self.opcode]

    @property
    def opargs(self):
        """Overrideable property

        opargs will be composed of different bits for different instructions.
        """
        return self._opargs

    def __len__(self):
        "Compute byte length of instruction."
        # In wordcode, an instruction is op, arg, each taking one
        # byte. If we have more than zero or one arg, we use
        # EXTENDED_ARG instructions to carry the other args, each
        # again two bytes.
        return 2 + 2 * len(self.opargs[1:])

    def __str__(self):
        me = self.__dict__.copy()
        del me["block"], me["opcode"]
        return f"Instruction({self.line_number}: {self.name}, {me})"

    def is_abs_jump(self):
        "True if opcode is an absolute jump."
        return self.opcode in opcode.hasjabs

    def is_rel_jump(self):
        "True if opcode is a relative jump."
        return self.opcode in opcode.hasjrel

    def is_jump(self):
        "True for any kind of jump."
        return self.is_abs_jump() or self.is_rel_jump()

    def __bytes__(self):
        "Generate wordcode."
        code = []
        for arg in self.opargs[:-1]:
            code.append(self.EXT_ARG_OPCODE)
            code.append(arg)
        code.append(self.opcode)
        code.append(self.opargs[-1])
        return bytes(code)

class PyVMInstruction(Instruction):
    "For basic PyVM instructions."
    def __init__(self, op, block, **kwargs):
        opargs = kwargs["opargs"]
        del kwargs["opargs"]
        super().__init__(op, block, **kwargs)
        self._opargs = opargs

class CompareOpInstruction(Instruction):
    "Specialized behavior for COMPARE_OP_REG."
    def __init__(self, op, block, **kwargs):
        self.source1 = kwargs["source1"]
        del kwargs["source1"]
        self.source2 = kwargs["source2"]
        del kwargs["source2"]
        self.dest = kwargs["dest"]
        del kwargs["dest"]
        self.compare_op = kwargs["compare_op"]
        del kwargs["compare_op"]
        super().__init__(op, block, **kwargs)

    @property
    def opargs(self):
        return (self.dest, self.source1, self.source2, self.compare_op)

class BuildSeqInstruction(Instruction):
    "Specialized behavior for sequence construction operations."
    def __init__(self, op, block, **kwargs):
        self.length = kwargs["length"]
        del kwargs["length"]
        self.dest = kwargs["dest"]
        del kwargs["dest"]
        super().__init__(op, block, **kwargs)

    @property
    def opargs(self):
        return (self.dest, self.length)

class ExtendSeqInstruction(Instruction):
    "Specialized behavior for LIST_EXTEND operation."
    # dest is modified in-place.
    def __init__(self, op, block, **kwargs):
        self.source1 = kwargs["source1"]
        del kwargs["source1"]
        self.dest = kwargs["dest"]
        del kwargs["dest"]
        super().__init__(op, block, **kwargs)

    @property
    def opargs(self):
        return (self.dest, self.source1)

class NOPInstruction(Instruction):
    "nop"
    pass
