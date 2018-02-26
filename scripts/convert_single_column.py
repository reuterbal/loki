import click as cli
import re
from collections import OrderedDict, defaultdict, Iterable
from copy import deepcopy

from ecir import (FortranSourceFile, Visitor, flatten, chunks,
                  Variable, Type, DerivedType, Declaration, FindNodes,
                  Statement)


class FindLoops(Visitor):

    def __init__(self, target_var):
        super(FindLoops, self).__init__()

        self.target_var = target_var

    def visit_Node(self, o):
        children = tuple(self.visit(c) for c in o.children)
        return tuple(c for c in children if c is not None)

    def visit_tuple(self, o):
        children = tuple(self.visit(c) for c in o)
        return tuple(c for c in children if c is not None)

    visit_list = visit_Node

    def visit_Loop(self, o):
        lines = o._source.splitlines(keepends=True)
        if self.target_var == o.variable:
            # Loop is over target dimension
            return (o, )
        elif o.body is not None:
            # Recurse over children to find target
            children = tuple(self.visit(c) for c in flatten(o.body))
            children = tuple(c for c in flatten(children) if c is not None)
            return children
        else:
            return ()


def generate_signature(name, arguments):
    """
    Generate subroutine signature from a given list of arguments
    """
    arg_names = list(chunks([a.name for a in arguments], 6))
    dummies = ', &\n & '.join(', '.join(c) for c in arg_names)
    return 'SUBROUTINE %s &\n & (%s)\n' % (name, dummies)


def generate_interface(filename, name, arguments, imports):
    """
    Generate the interface file for a given Subroutine.
    """
    signature = generate_signature(name, arguments)
    interface = 'INTERFACE\n%s' % signature

    # Collect unknown symbols that we might need to import
    undefined = set()
    anames = [a.name for a in arguments]
    for a in arguments:
        # Add potentially unkown TYPE and KIND symbols to 'undefined'
        if a.type.name.upper() not in ['REAL', 'INTEGER', 'LOGICAL', 'COMPLEX']:
            undefined.add(a.type)
        if a.type.kind and not a.type.kind.isdigit():
            undefined.add(a.type.kind)
        # Add (pure) variable dimensions that might be defined elsewhere
        undefined.update([str(d) for d in a.dimensions
                          if isinstance(d, Variable) and d not in anames])

    # Write imports for undefined symbols from external modules
    for use in imports:
        symbols = [s for s in use.symbols if s in undefined]
        if len(symbols) > 0:
            interface += 'USE %s, ONLY: %s\n' % (use.module, ', '.join(symbols))

    # Add type declarations for all arguments
    for arg in arguments:
        interface += '%s%s, INTENT(%s) :: %s\n' % (
            arg.type.name, ('(KIND=%s)' % arg.type.kind) if arg.type.kind else '',
            arg.type.intent.upper(), str(arg))
    interface += 'END SUBROUTINE %s\nEND INTERFACE\n' % name

    # And finally dump the generated string to file
    print("Writing interface to %s" % filename)
    with open(filename, 'w') as file:
        file.write(interface)


@cli.command()
@cli.option('--source', '-s', type=cli.Path(),
            help='Source file to convert.')
@cli.option('--source-out', '-so', type=cli.Path(),
            help='Path for generated source output.')
@cli.option('--driver', '-d', type=cli.Path(), default=None,
            help='Driver file to convert.')
@cli.option('--driver-out', '-do', type=cli.Path(), default=None,
            help='Path for generated driver output.')
@cli.option('--interface', '-intfb', type=cli.Path(), default=None,
            help='Path to auto-generate and interface file')
@cli.option('--typedef', '-t', type=cli.Path(), multiple=True,
            help='Path for additional source file(s) containing type definitions')
@cli.option('--mode', '-m', type=cli.Choice(['onecol', 'claw']), default='onecol')
@cli.option('--strip-signature/--no-strip-signature', default=True)
def convert(source, source_out, driver, driver_out, interface, typedef, mode, strip_signature):

    # Read additional derived types from typedef modules
    derived_types = {}
    for tfile in typedef:
        t_source = FortranSourceFile(tfile)
        t_mod = t_source.modules[0]
        for derived in t_mod._spec:
            if isinstance(derived, DerivedType):
                # TODO: Need better __hash__ for (derived) types
                derived_types[derived.name.upper()] = derived

    # Read the primary source routine
    f_source = FortranSourceFile(source)
    routine = f_source.subroutines[0]

    target_dim = 'KLON'  # Name of the target dimension
    target_var = 'JL'  # Name of the target iteration variable
    target_sizes = ['KIDIA', 'KFDIA', 'KLON']  # Variables to strip from signatures
    target_variables = [target_dim] + target_sizes

    ####  Remove target loops  ####

    # It's important to do this first, as the IR on the `routine`
    # object is not updated when the source changes...
    # TODO: Fully integrate IR with source changes...
    finder = FindLoops(target_var=target_var)
    target_loops = flatten(finder.visit(routine._ir))
    for target in target_loops:
        # Get loop body and drop two leading chars for unindentation
        lines = target._source.splitlines(keepends=True)[1:-1]
        lines = ''.join([line.replace('  ', '', 1) for line in lines])
        routine.body._source = routine.body._source.replace(target._source, lines)

    ####  Signature and interface adjustments  ####

    # We deep-copy arguments to make sure we are not affecting the
    # variable dimensions used in the later parts for regex replacement.
    arguments = deepcopy(routine.arguments)

    # Detect argument variables with derived types in the signature
    # that use the target diemnsion. For those types we explicitly unroll
    # the subtypes used in the signature and adjust caller and callee.
    derived_arg_map = OrderedDict()
    derived_arg_repl = {}
    for arg in arguments:
        if arg.type.name.upper() in derived_types:
            new_vars = []
            # TODO: Need to define __key/__hash__ for Variables and (Derived)Types
            derived = derived_types[arg.type.name.upper()]
            for type_var in derived.variables:
                # Check if variable has the target dimension and is used in routine
                t_str = '%s%%%s' % (arg.name, type_var.name)
                if target_dim in type_var.dimensions and t_str in routine.body._source:
                    new_name = '%s_%s' % (arg.name, type_var.name)
                    new_type = Type(name=type_var.type.name, kind=type_var.type.kind,
                                    intent=arg.type.intent)
                    new_vars.append(Variable(name=new_name, type=new_type,
                                             dimensions=type_var.dimensions))

                    # Record the string-replacement for the body
                    derived_arg_repl[t_str] = new_name

            # Derive index on-the-fly (multi-element insertions change indices!)
            # and update the argument list with unrolled arguments.
            idx = arguments.index(arg)
            # Store replacement for later declaration adjustment
            derived_arg_map[arg] = new_vars
            arguments[idx:idx+1] = new_vars

    # Now we replace the declarations for the previously derived arguments
    # Note: Re-generation from AST would probably be cleaner...
    declarations = FindNodes(Declaration).visit(routine._spec)
    for derived_arg, new_args in derived_arg_map.items():
        for decl in declarations:
            if derived_arg in decl.variables:
                # A simple sanity check...
                decl.variables.remove(derived_arg)
                if len(decl.variables) > 0:
                    raise NotImplementedError('More than one derived argument per declaration found!')

                # Replace derived argument declaration with new declarations
                new_decls = []
                for arg in new_args:
                    new_decl = '%s%s' % (arg.type.name,
                                         '(KIND=%s)' % arg.type.kind.upper() if arg.type.kind else '')
                    new_decl += ', INTENT(%s)' % arg.type.intent.upper() if arg.type.intent else ''
                    new_decl += ' :: %s' % str(arg)
                    # Assemble new declarations
                    new_decls.append(new_decl)

                new_string = '\n'.join(new_decls) + '\n'
                routine.declarations._source = routine.declarations._source.replace(decl._source,
                                                                                    new_string)

    # And finally, replace all occurences of derived sub-types with unrolled ones
    routine.body.replace(derived_arg_repl)

    # Strip the target dimension from arguments
    if strip_signature:
        arguments = [a for a in arguments if a.name not in target_variables]

    # Remove the target dimensions from our input arguments
    for a in arguments:
        a.dimensions = tuple(d for d in a.dimensions if target_dim not in str(d))

    if interface:
        # Generate the interface file associated with this routine
        generate_interface(filename=interface, name=routine.name,
                           arguments=arguments, imports=routine.imports)

    if strip_signature:
        # Generate new signature and replace the old one in file
        re_sig = re.compile('SUBROUTINE\s+%s.*?\(.*?\)' % routine.name, re.DOTALL)
        signature = re_sig.findall(routine._source)[0]
        new_signature = generate_signature(routine.name, arguments)
        routine.declarations._source = routine.declarations._source.replace(signature, new_signature)

        # Strip target sizes from declarations
        for v in routine.arguments:
            if v.name in target_sizes:
                routine.declarations._source = routine.declarations._source.replace(v._source, '')

        # Strip target loop variable
        line = routine._variables[target_var]._source
        new_line = line.replace('%s, ' % target_var, '')
        routine.declarations._source = routine.declarations._source.replace(line, new_line)

    ####  Index replacements  ####

    # Strip all target iteration indices
    routine.body.replace({'(%s,' % target_var: '(', '(%s)' % target_var: ''})

    # Find all variables affected by the transformation
    # Note: We assume here that the target dimension is matched
    # exactly in v.dimensions!
    variables = [v for v in routine.variables if target_dim in v.dimensions]
    for v in variables:
        # Target is a vector, we now promote it to a scalar
        promote_to_scalar = len(v.dimensions) == 1
        new_dimensions = list(v.dimensions)
        new_dimensions.remove(target_dim)

        # Strip target dimension from declarations and body (for ALLOCATEs)
        old_dims = '(%s)' % ','.join(str(d) for d in v.dimensions)
        new_dims = '' if promote_to_scalar else '(%s)' % ','.join(str(d)for d in new_dimensions)
        routine.declarations.replace({old_dims: new_dims})
        routine.body.replace({old_dims: new_dims})

        # Strip all colon indices for leading dimensions
        # TODO: Could do this in a smarter, more generic way...
        if promote_to_scalar:
            routine.body.replace({'%s(:)' % v.name: '%s' % v.name})
        else:
            routine.body.replace({'%s(:,' % v.name: '%s(' % v.name})

        if v.type.allocatable:
            routine.declarations.replace({'%s(:,' % v.name: '%s(' % v.name})

    ####  Hacks that for specific annoyances in the CLOUDSC dwarf  ####

    variables = [v for v in routine.variables
                 if 'KFDIA' in ','.join(str(d) for d in v.dimensions)
                 or 'KLON' in ','.join(str(d) for d in v.dimensions)]
    for v in variables:
        routine.declarations.replace({'%s(KFDIA-KIDIA+1)' % v.name: '%s' % v.name,
                                      '%s(KFDIA-KIDIA+1,' % v.name: '%s(' % v.name,
                                      '%s(2*(KFDIA-KIDIA+1))' % v.name: '%s(2)' % v.name,
                                      '%s(2*KLON)' % v.name: '%s(2)' % v.name,
                                  })
        # TODO: This one is hacky and assumes we always process FULL BLOCKS!
        # We effectively treat block_start:block_end v.nameiables as (:)
        routine.body.replace({'%s(JL-KIDIA+1,' % v.name: '%s(' % v.name,
                              '%s(JL-KIDIA+1)' % v.name: '%s' % v.name,
                              '%s(KIDIA:KFDIA,' % v.name: '%s(' % v.name,
                              '%s(KIDIA:KFDIA)' % v.name: '%s' % v.name,
                              '%s(KIDIA,' % v.name: '%s(' % v.name,
                              '%s(KIDIA)' % v.name: '%s' % v.name,
                         })
    # And finally we have no shame left... :(
    routine.body.replace({'Z_TMPK(1,JK)': 'Z_TMPK(JK)',
                          '& (KIDIA,    KFDIA,   KLON,     KLEV,    IK,&':
                          '& (    1,        1,      1,     KLEV,    IK,&',
                          'JLEN=KFDIA-KIDIA+1': 'JLEN=1',
                          'KFDIA-KIDIA+1)': '1)',
                      })

    ####  CLAW-specific modifications  ####

    if mode == 'claw':
        # Prepend CLAW directives to subroutine body
        scalars = [v.name.lower() for v in routine.arguments
                   if len(v.dimensions) == 1]
        directives = '!$claw define dimension jl(1:klon) &\n'
        directives += '!$claw parallelize &\n'
        directives += '!$claw scalar(%s)\n\n\n' % ', '.join(scalars)
        routine.body._source = directives + routine.body._source

        # Wrap subroutine in a module
        f_source._pre._source += 'MODULE cloudsc_mod\ncontains\n'
        f_source._post._source += 'END MODULE'

    print("Writing to %s" % source_out)
    f_source.write(source_out)

    # Now let's process the driver/caller side
    if driver is not None:
        f_driver = FortranSourceFile(driver)

        # Process individual calls to our target routine
        # re_call = re.compile('CALL %s[\s\&\(].*?\)\s*?\n' % routine.name, re.DOTALL)
        # for call in re_call.findall(f_driver._raw_source):
        #     # Create the outer loop from the first two arguments
        #     pass
            
        print("Writing to %s" % driver_out)
        f_driver.write(driver_out)

if __name__ == "__main__":
    convert()
