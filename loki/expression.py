from abc import ABCMeta, abstractproperty

from loki.visitors import GenericVisitor, Visitor
from loki.tools import flatten, as_tuple

__all__ = ['Expression', 'Operation', 'Literal', 'Variable', 'Cast', 'Index',
           'RangeIndex', 'ExpressionVisitor', 'LiteralList', 'FindVariables']


class ExpressionVisitor(GenericVisitor):

    def visit_Statement(self, o, **kwargs):
        return tuple([self.visit(o.target, **kwargs), self.visit(o.expr, **kwargs)])

    def visit_Expression(self, o, **kwargs):
        return tuple(self.visit(c, **kwargs) for c in o.children)


class FindVariables(ExpressionVisitor, Visitor):
    """
    A dedicated visitor to collect all variables used in an IR tree.

    Note: With `unique=False` all variables instanecs are traversed,
    allowing us to change them in-place. Conversely, `unique=True`
    returns a :class:`set` of unique :class:`Variable` objects that
    can be used to check if a particular variable is used in a given
    context.

    Note: :class:`Variable` objects are not recursed on in themselves.
    That means that individual component variables or dimension indices
    are not traversed or included in the final :class:`set`.
    """

    def __init__(self, unique=True):
        super(FindVariables, self).__init__()
        self.unique = unique

    default_retval = tuple

    def visit_tuple(self, o):
        vars = flatten(self.visit(c) for c in o)
        return set(vars) if self.unique else vars

    visit_list = visit_tuple

    def visit_Variable(self, o):
        dims = flatten(self.visit(d) for d in o.dimensions)
        return set(dims + [o]) if self.unique else tuple(dims + [o])

    def visit_Expression(self, o):
        vars = flatten(self.visit(c) for c in o.children)
        return set(vars) if self.unique else vars

    visit_InlineCall = visit_Expression

    def visit_Statement(self, o, **kwargs):
        vars = as_tuple(self.visit(o.expr, **kwargs))
        vars += as_tuple(self.visit(o.target))
        return set(vars) if self.unique else as_tuple(vars)

    def visit_Loop(self, o, **kwargs):
        vars = flatten(self.visit(o.variable) for c in o.children)
        vars += flatten(self.visit(o.bounds.lower) for c in o.children)
        vars += flatten(self.visit(o.bounds.upper) for c in o.children)
        vars += flatten(self.visit(o.bounds.step) for c in o.children)
        vars += flatten(self.visit(o.body) for c in o.children)
        return set(vars) if self.unique else as_tuple(vars)


class Expression(object):
    """
    Base class for aithmetic and logical expressions.

    Note: :class:`Expression` objects are not part of the IR hierarchy,
    because re-building each individual expression tree during
    :class:`Transformer` passes can quickly become much more costly
    than re-building the control flow structures.
    """

    __metaclass__ = ABCMeta

    def __init__(self, source=None):
        self._source = source

    @abstractproperty
    def expr(self):
        """
        Symbolic representation - might be used in this raw form
        for code generation.
        """
        pass

    @abstractproperty
    def type(self):
        """
        Data type of (sub-)expressions.

        Note, that this is the pure data type (eg. int32, float64),
        not the full variable declaration type (allocatable, pointer,
        etc.). This is so that we may reason about it recursively.
        """
        pass

    def __repr__(self):
        return self.expr

    @property
    def children(self):
        return ()


class Operation(Expression):

    def __init__(self, ops, operands, parenthesis=False, source=None):
        super(Operation, self).__init__(source=source)
        self.ops = as_tuple(ops)
        self.operands = as_tuple(operands)
        self.parenthesis = parenthesis

    @property
    def expr(self):
        if len(self.ops) == 1 and len(self.operands) == 1:
            # Special case: a unary operator
            return '%s%s' % (self.ops[0], self.operands[0])

        s = str(self.operands[0])
        s += ''.join(['%s%s' % (o, str(e)) for o, e in zip(self.ops, self.operands[1:])])
        return ('(%s)' % s) if self.parenthesis else s

    @property
    def type(self):
        types = [o.type for o in self.operands]
        assert(all(types == types[0]))
        return types[0]

    @property
    def children(self):
        return self.operands

    def __key(self):
        return (self.ops, self.operands, self.parenthesis)

    def __hash__(self):
        return hash(self.__key())

    def __eq__(self, other):
        # Allow direct comparisong to string and other Index objects
        if isinstance(other, str):
            return self.expr.upper() == other.upper()
        elif isinstance(other, Operation):
            return self.__key() == other.__key()
        else:
            return super(Operation, self).__eq__(other)


class Literal(Expression):

    def __init__(self, value, kind=None, type=None, source=None):
        super(Literal, self).__init__(source=source)
        self.value = value
        self.kind = kind
        self._type = type

    @property
    def expr(self):
        return self.value if self.kind is None else '%s_%s' % (self.value, self.kind)

    @property
    def type(self):
        return self._type

    def __key(self):
        return (self.value, self.kind, self._type)

    def __hash__(self):
        return hash(self.__key())

    def __eq__(self, other):
        # Allow direct comparisong to string and other Index objects
        if isinstance(other, str):
            return self.value.upper() == other.upper()
        elif isinstance(other, Literal):
            return self.__key() == other.__key()
        else:
            return super(Literal, self).__eq__(other)


class LiteralList(Expression):

    def __init__(self, values, source=None):
        self.values = values

    @property
    def expr(self):
        return '(/%s/)' % ', '.join(str(v) for v in self.values)


class Variable(Expression):

    def __init__(self, name, type=None, shape=None, dimensions=None,
                 ref=None, initial=None, source=None):
        super(Variable, self).__init__(source=source)
        self._source = source

        self.name = name
        self._type = type
        self._shape = shape
        self.ref = ref  # Derived-type parent object
        self.dimensions = dimensions or ()
        self.initial = initial

    @property
    def expr(self):
        idx = ''
        if self.dimensions is not None and len(self.dimensions) > 0:
            idx = '(%s)' % ','.join([str(i) for i in self.dimensions])
        ref = '' if self.ref is None else '%s%%' % str(self.ref)
        return '%s%s%s' % (ref, self.name, idx)

    @property
    def type(self):
        return self._type

    @property
    def shape(self):
        """
        Original allocated shape of the variable as a tuple of dimensions.
        """
        return self._shape

    def __key(self):
        return (self.name, self.type, self.dimensions, self.ref)

    def __hash__(self):
        return hash(self.__key())

    def __eq__(self, other):
        # Allow direct comparison to string and other Variable objects
        if isinstance(other, str):
            return str(self).upper() == other.upper()
        elif isinstance(other, Variable):
            return self.__key() == other.__key()
        else:
            return super(Variable, self).__eq__(other)

    @property
    def children(self):
        c = self.dimensions
        if self.ref is not None:
            c += (self.ref, )
        return c


class InlineCall(Expression):
    """
    Internal representation of an in-line function call
    """
    def __init__(self, name, arguments=None, kwarguments=None):
        self.name = name
        self.arguments = arguments
        self.kwarguments = kwarguments

    @property
    def expr(self):
        kwargs = tuple('%s=%s' % (k, v) for k, v in as_tuple(self.kwarguments))
        args = as_tuple(self.arguments) + kwargs
        return '%s(%s)' % (self.name, ','.join(str(a) for a in args))

    @property
    def children(self):
        return self.arguments


class Cast(Expression):
    """
    Internal representation of a data cast to a psecific type.
    """

    def __init__(self, expr, type):
        self._expr = expr
        self._type = type

    @property
    def expr(self):
        return '%s' % self._expr

    @property
    def type(self):
        return self._type

    @property
    def children(self):
        return as_tuple(self._expr)


class Index(Expression):

    def __init__(self, name):
        self.name = name

    @property
    def expr(self):
        return '%s' % self.name

    def __key(self):
        return (self.name)

    def __hash__(self):
        return hash(self.__key())

    @property
    def type(self):
        # TODO: Some common form of `INT`, maybe?
        return None

    def __eq__(self, other):
        # Allow direct comparisong to string and other Index objects
        if isinstance(other, str):
            return self.name.upper() == other.upper()
        elif isinstance(other, Index):
            return self.name == other.name
        else:
            return super(Index, self).__eq__(other)


class RangeIndex(Expression):

    def __init__(self, lower, upper, step=None):
        self.lower = lower
        self.upper = upper
        self.step = step

    @property
    def expr(self):
        step = '' if self.step is None else ':%s' % self.step
        lower = '' if self.lower is None else self.lower
        upper = '' if self.upper is None else self.upper
        return '%s:%s%s' % (lower, upper, step)

    @property
    def children(self):
        return self.lower, self.upper, self.step

    def __key(self):
        return (self.lower, self.upper, self.step)

    def __hash__(self):
        return hash(self.__key())

    def __eq__(self, other):
        # Short-cut for "trivial ranges", ie. `1:v:1` -> `v`
        if (self.lower is None or self.lower == '1') and (self.step is None or self.step == '1'):
            if self.upper is not None:
                return self.upper == other

        # Basic comparison agaisnt strings or other ranges
        if isinstance(other, str):
            return self.expr.upper() == other.upper()
        elif isinstance(other, RangeIndex):
            return self.__key() == other.__key()
        else:
            return super(RangeIndex, self).__eq__(other)