"""Compare the executable instructions of isolated single-line function edits."""

import ast
import dis
import marshal
import types


def executable(code):
    instructions = []
    for instruction in dis.get_instructions(code):
        argument = instruction.arg
        if instruction.opcode in dis.hasconst and argument is not None:
            value = code.co_consts[argument]
            argument = (
                executable(value)
                if isinstance(value, types.CodeType)
                else marshal.dumps(value)
            )
        instructions.append((instruction.offset, instruction.opcode, argument))
    return (
        (
            code.co_consts[0]
            if code.co_consts and isinstance(code.co_consts[0], str)
            else None
        ),
        tuple(instructions),
        code.co_exceptiontable,
        code.co_names,
        code.co_varnames,
        code.co_freevars,
        code.co_cellvars,
        code.co_flags,
        code.co_argcount,
        code.co_posonlyargcount,
        code.co_kwonlyargcount,
        code.co_name,
        code.co_qualname,
        tuple(code.co_lines()),
    )


def unchanged(original, modified):
    if not isinstance(original, ast.FunctionDef) or not isinstance(
        modified, ast.FunctionDef
    ):
        return False

    def compile_function(node):
        module = ast.Module(body=[node], type_ignores=[])
        return compile(module, "<selector>", "exec", dont_inherit=True)

    try:
        baseline = compile_function(original)
        patched = compile_function(modified)
        return executable(baseline) == executable(patched)
    except (ValueError, TypeError, SyntaxError):
        return False
