import sys
import os
import warnings
import re
import docstring_parser

from wfl.configset import ConfigSet, OutputSpec
from .pool import do_in_pool
from .remote import do_remotely
from .autoparainfo import AutoparaInfo
from .utils import get_remote_info

# NOTES from a previous implementation that does not work with sphinx docstring parsing
# may not be exactly correct, but can definitely be made to work (except sphinx)
#
# It is possible to define autoparallelize so that the decorator functionality can be done with
#
#     parallelized_op = autoparallelize(op, "parallelized_op", "Atoms",. [autoparallelize_arg_1 = ... ])
#
# using functools.partial-based decorator (with ideas from topic 4 of
# https://pythonicthoughtssnippets.github.io/2020/08/09/PTS13-rethinking-python-decorators.html).
# Works OK and can be pickled, but ugly handling of docstring, and no handling of function signature.
#
# this is done by renaming the current "autoparallelize" to "_autoparallelize_wrapper", and defining a new "autoparallelize"
# as something like
#
#     def autoparallelize(op, op_name, input_iterator_contents, **autoparallelize_kwargs):
#         f = functools.partial(_autoparallelize_wrapper, op, **autoparallelize_kwargs)
#         f.__name__ = op_name
#         f.__doc__ = autopara_docstring(op.__doc__, input_iterator_contents)
#         return f
#
# it also requires that the code parsing the stack trace to match up the RemoteInfo dict
# detects the presence of "autoparallelize" in the stack trace, and instead uses
#
#     inspect.getfile(op) + "::" + op.__name__
#
# instead of the stack trace file and function
#
# sphinx docstring parsing cannot handle this, though, because the module associated with
# the "parallelized_op" symbol is that of "autoparallelize", rather than the python file in which
# it is actually defined.  As a result, sphinx assumes it's just some imported symbol and does
# not include its docstring.  There does not appear to be any way to override that on a
# per-symbol basis in current sphinx versions.

_autopara_docstring_params_pre = [
    [["param", "inputs"],
     "input quantities of type {input_iterable_type}",
     "inputs", "iterable({input_iterable_type})", False, None],
    [["param", "outputs"],
     "where to write output atomic configs, or None for no output (i.e. only side-effects)",
     "outputs", "OutputSpec or None", False, None]
]

_autopara_docstring_params_post = [
    [["param", "autopara_info"],
     "information for automatic parallelization",
     "autopara_info", "AutoParaInfo / dict", True, None]
]

_autopara_docstring_returns = [["returns"],
                               "output configs", "ConfigSet",
                               False, "co"]


def autoparallelize_docstring(wrapped_func, wrappable_func, input_iterable_type, input_arg=0):
    parsed = docstring_parser.parse(wrappable_func.__doc__)

    # find input arg
    input_arg_i = -1
    param_i = -1
    for p_i, p in enumerate(parsed.meta):
        if isinstance(p, docstring_parser.DocstringParam):
            param_i += 1
            if (isinstance(input_arg, int) and param_i == input_arg) or (isinstance(input_arg, str) and p.arg_name == input_arg):
                input_arg_i = p_i

    # replace input_arg with pre params
    del parsed.meta[input_arg_i]
    for param_list in reversed(_autopara_docstring_params_pre):
        param_list = [p.format(**{"input_iterable_type": input_iterable_type}) if isinstance(p, str) else p for p in param_list]
        parsed.meta.insert(input_arg_i, docstring_parser.DocstringParam(*param_list))

    # find last arg
    last_arg_i = -1
    for p_i, p in enumerate(parsed.meta):
        if isinstance(p, docstring_parser.DocstringParam):
            last_arg_i = p_i

    # add post to end
    for param_list in reversed(_autopara_docstring_params_post):
        param_list = [p.format(**{"input_iterable_type": input_iterable_type}) if isinstance(p, str) else p for p in param_list]
        parsed.meta.insert(last_arg_i + 1, docstring_parser.DocstringParam(*param_list))

    # find returns
    returns_i = None
    for p_i, p in enumerate(parsed.meta):
        if isinstance(p, docstring_parser.DocstringReturns):
            returns_i = p_i
    if returns_i is not None:
        # replace returns
        parsed.meta[returns_i] =  docstring_parser.DocstringReturns(*_autopara_docstring_returns)
    else:
        # append returns
        parsed.meta.append(docstring_parser.DocstringReturns(*_autopara_docstring_returns))

    wrapped_func.__doc__ = docstring_parser.compose(parsed)


def autoparallelize(func, *args, def_autopara_info={}, **kwargs):
    """autoparallelize a function

    Use by defining function "op" which takes an input iterable and returns list of configs, and _after_ do

    .. code-block:: python

        def autoparallelized_op(*args, **kwargs):
            return autoparallelize(op, *args, def_autopara_info={"autoparallelize_keyword_param_1": val, "autoparallelize_keyword_param_2": val, ... }, **kwargs )
        autoparallelized_op.doc = autopara_docstring(op.__doc__, "iterable_contents")

    The autoparallelized function can then be called with 

    .. code-block:: python

        parallelized_op(inputs, outputs, [args of op], autopara_info=AutoparaInfo(arg1=val1, ...), [kwargs of op])


    Parameters
    ----------
    func: function
        function to wrap in _autoparallelize_ll()

    *args: list
        positional arguments to func, plus optional first or first and second inputs (iterable) and outputs (OutputSpec) arguments to wrapped function

    def_autopara_info: dict, default {}
        dict with default values for AutoparaInfo constructor keywords setting default autoparallelization info

    **kwargs: dict
        keyword arguments to func, plus optional inputs (iterable), outputs (OutputSpec), and  autopara_info (AutoparaInfo)

    Returns
    -------
    wrapped_func_out: results of calling the function wrapped in autoparallelize via _autoparallelize_ll
    """

    # copy kwargs and args so they can be modified for call to autoparallelize
    kwargs = kwargs.copy()
    args = list(args)
    if 'inputs' in kwargs:
        # inputs is keyword, outputs must be too, any positional args to func are unchanged
        inputs = kwargs.pop('inputs')
        outputs = kwargs.pop('outputs')
    else:
        # inputs is positional, remove it from args to func
        inputs = args.pop(0)
        if 'outputs' in kwargs:
            outputs = kwargs.pop('outputs')
        else:
            # outputs is also positional, remote it from func args as well
            outputs = args.pop(0)

    # create autopara_info from explicitly passed AutoparaInfo object, otherwise an empty object
    autopara_info = kwargs.pop("autopara_info", AutoparaInfo())
    if isinstance(autopara_info, dict):
        autopara_info = AutoparaInfo(**autopara_info)
    # update values, if any are not set, with defaults that were set by decorating code
    autopara_info.update_defaults(def_autopara_info)

    return _autoparallelize_ll(autopara_info.num_python_subprocesses, autopara_info.num_inputs_per_python_subprocess,
                               inputs, outputs, func, autopara_info.iterable_arg,
                               autopara_info.skip_failed, autopara_info.initializer, autopara_info.remote_info,
                               autopara_info.remote_label, autopara_info.hash_ignore,
                               *args, **kwargs)

# do we want to allow for ops that only take singletons, not iterables, as input, maybe with num_inputs_per_python_subprocess=0 ?
# that info would have to be passed down to _wrapped_autopara_wrappable so it passes a singleton rather than a list into op
#
# some ifs (int positional vs. str keyword) could be removed if we required that the iterable be passed into a kwarg.

def _autoparallelize_ll(num_python_subprocesses, num_inputs_per_python_subprocess,
                        iterable, outputspec, op, iterable_arg,
                        skip_failed, initializer, remote_info,
                        remote_label, hash_ignore,
                        *args, **kwargs):
    """Parallelize some operation over an iterable

    Parameters
    ----------
    num_python_subprocesses: int, default os.environ['WFL_NUM_PYTHON_SUBPROCESSES']
        number of processes to parallelize over, 0 for running in serial
    num_inputs_per_python_subprocess: int, default 1
        number of items from iterable to pass to kach invocation of operation
    iterable: iterable, default None
        iterable to loop over, often ConfigSet but could also be other things like range()
    outputspec: OutputSpec, default None
        object containing returned Atoms objects
    op: callable
        function to call with each chunk
    iterable_arg: int or str, default 0
        positional argument or keyword argument to place iterable items in when calling op
    skip_failed: bool, default True
        skip function calls that return None
    initializer: (callable, list), default (None, ())
        function to call at beginning of each thread and its positional arguments
    remote_info: RemoteInfo, default content of env var WFL_EXPYRE_INFO
        information for running on remote machine.  If None, use WFL_EXPYRE_INFO env var, as
        json file if string, as RemoteInfo kwargs dict if keys include sys_name, or as dict of
        RemoteInfo kwrgs with keys that match end of stack trace with function names separated by '.'.
    remote_label: str, default None
        remote_label to use for operation, to match to remote_info dict keys.  If none, use calling routine filename '::' calling function
    hash_ignore: list(str), default []
        arguments to ignore when doing remot executing and computing hash of function to determine
        if it's already done
    args: list
        positional arguments to op
    kwargs: dict
        keyword arguments to op

    Returns
    -------
    ConfigSet containing returned configs if outputspec is not None, otherwise None
    """
    remote_info = get_remote_info(remote_info, remote_label)

    if isinstance(iterable_arg, int):
        assert len(args) >= iterable_arg
        # otherwise not enough args were provided

    if outputspec is not None:
        if not isinstance(outputspec, OutputSpec):
            raise RuntimeError(f'autoparallelize requires outputspec be None or OutputSpec, got {type(outputspec)}')
        if outputspec.done():
            sys.stderr.write(f'Returning before {op} since output is done\n')
            return outputspec.to_ConfigSet()

    if remote_info is not None:
        out = do_remotely(remote_info, hash_ignore, num_inputs_per_python_subprocess, iterable, outputspec,
                          op, iterable_arg, skip_failed, initializer, args, kwargs)
    else:
        out = do_in_pool(num_python_subprocesses, num_inputs_per_python_subprocess, iterable, outputspec, op, iterable_arg,
                         skip_failed, initializer, args, kwargs)

    return out
