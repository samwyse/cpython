
import opcode

from . import DISPATCH
from .instructions import Instruction

def function(self, instr, block):
    "dst <- function(...)"
    oparg = instr.opargs[0] # All PyVM opcodes have a single oparg
    nargs = oparg
    dest = self.top() - nargs
    for _ in range(nargs):
        _x = self.pop()
    return CallInstruction(opcode.opmap['CALL_FUNCTION_REG'],
                           block, nargs=nargs, dest=dest)
DISPATCH[opcode.opmap['CALL_FUNCTION']] = function

def function_kw(self, instr, block):
    oparg = instr.opargs[0] # All PyVM opcodes have a single oparg
    nargs = oparg
    nreg = self.top()
    dest = self.top() - nargs - 1
    #print(nargs, nreg, dest)
    for _ in range(nargs + 1):
        _x = self.pop()
    return CallInstructionKW(opcode.opmap['CALL_FUNCTION_KW_REG'],
                             block, nargs=nargs, nreg=nreg, dest=dest)
DISPATCH[opcode.opmap['CALL_FUNCTION_KW']] = function_kw

class CallInstruction(Instruction):
    "Basic CALL_FUNCTION_REG."
    def __init__(self, op, block, **kwargs):
        self.nargs = kwargs["nargs"]
        del kwargs["nargs"]
        self.dest = kwargs["dest"]
        del kwargs["dest"]
        super().__init__(op, block, **kwargs)

    @property
    def opargs(self):
        return (self.dest, self.nargs)

class CallInstructionKW(Instruction):
    "Basic CALL_FUNCTION_KW_REG."
    def __init__(self, op, block, **kwargs):
        self.nargs = kwargs["nargs"]
        del kwargs["nargs"]
        self.nreg = kwargs["nreg"]
        del kwargs["nreg"]
        self.dest = kwargs["dest"]
        del kwargs["dest"]
        super().__init__(op, block, **kwargs)

    @property
    def opargs(self):
        return (self.dest, self.nreg, self.nargs)
