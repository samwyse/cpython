
/* interpreters module */
/* low-level access to interpreter primitives */
#ifndef Py_BUILD_CORE_BUILTIN
#  define Py_BUILD_CORE_MODULE 1
#endif

#include "Python.h"
// XXX This module should not rely on internal API.
#include "pycore_frame.h"
#include "pycore_pystate.h"       // _PyThreadState_GET()
#include "pycore_interpreteridobject.h"


#define MODULE_NAME "_xxsubinterpreters"


static const char *
_copy_raw_string(PyObject *strobj)
{
    const char *str = PyUnicode_AsUTF8(strobj);
    if (str == NULL) {
        return NULL;
    }
    char *copied = PyMem_RawMalloc(strlen(str)+1);
    if (copied == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    strcpy(copied, str);
    return copied;
}

static PyInterpreterState *
_get_current_interp(void)
{
    // PyInterpreterState_Get() aborts if lookup fails, so don't need
    // to check the result for NULL.
    return PyInterpreterState_Get();
}

static PyObject *
add_new_exception(PyObject *mod, const char *name, PyObject *base)
{
    assert(!PyObject_HasAttrString(mod, name));
    PyObject *exctype = PyErr_NewException(name, base, NULL);
    if (exctype == NULL) {
        return NULL;
    }
    int res = PyModule_AddType(mod, (PyTypeObject *)exctype);
    if (res < 0) {
        Py_DECREF(exctype);
        return NULL;
    }
    return exctype;
}

#define ADD_NEW_EXCEPTION(MOD, NAME, BASE) \
    add_new_exception(MOD, MODULE_NAME "." Py_STRINGIFY(NAME), BASE)

static int
_release_xid_data(_PyCrossInterpreterData *data, int ignoreexc)
{
    PyObject *exc;
    if (ignoreexc) {
        exc = PyErr_GetRaisedException();
    }
    int res = _PyCrossInterpreterData_Release(data);
    if (res < 0) {
        // XXX Fix this!
        /* The owning interpreter is already destroyed.
         * Ideally, this shouldn't ever happen.  (It's highly unlikely.)
         * For now we hack around that to resolve refleaks, by decref'ing
         * the released object here, even if its the wrong interpreter.
         * The owning interpreter has already been destroyed
         * so we should be okay, especially since the currently
         * shareable types are all very basic, with no GC.
         * That said, it becomes much messier once interpreters
         * no longer share a GIL, so this needs to be fixed before then. */
        _PyCrossInterpreterData_Clear(NULL, data);
        if (ignoreexc) {
            // XXX Emit a warning?
            PyErr_Clear();
        }
    }
    if (ignoreexc) {
        PyErr_SetRaisedException(exc);
    }
    return res;
}


/* module state *************************************************************/

typedef struct {
    /* exceptions */
    PyObject *RunFailedError;
} module_state;

static inline module_state *
get_module_state(PyObject *mod)
{
    assert(mod != NULL);
    module_state *state = PyModule_GetState(mod);
    assert(state != NULL);
    return state;
}

static int
traverse_module_state(module_state *state, visitproc visit, void *arg)
{
    /* exceptions */
    Py_VISIT(state->RunFailedError);

    return 0;
}

static int
clear_module_state(module_state *state)
{
    /* exceptions */
    Py_CLEAR(state->RunFailedError);

    return 0;
}


/* data-sharing-specific code ***********************************************/

struct _sharednsitem {
    const char *name;
    _PyCrossInterpreterData data;
};

static void _sharednsitem_clear(struct _sharednsitem *);  // forward

static int
_sharednsitem_init(struct _sharednsitem *item, PyObject *key, PyObject *value)
{
    item->name = _copy_raw_string(key);
    if (item->name == NULL) {
        return -1;
    }
    if (_PyObject_GetCrossInterpreterData(value, &item->data) != 0) {
        _sharednsitem_clear(item);
        return -1;
    }
    return 0;
}

static void
_sharednsitem_clear(struct _sharednsitem *item)
{
    if (item->name != NULL) {
        PyMem_RawFree((void *)item->name);
        item->name = NULL;
    }
    (void)_release_xid_data(&item->data, 1);
}

static int
_sharednsitem_apply(struct _sharednsitem *item, PyObject *ns)
{
    PyObject *name = PyUnicode_FromString(item->name);
    if (name == NULL) {
        return -1;
    }
    PyObject *value = _PyCrossInterpreterData_NewObject(&item->data);
    if (value == NULL) {
        Py_DECREF(name);
        return -1;
    }
    int res = PyDict_SetItem(ns, name, value);
    Py_DECREF(name);
    Py_DECREF(value);
    return res;
}

typedef struct _sharedns {
    Py_ssize_t len;
    struct _sharednsitem* items;
} _sharedns;

static _sharedns *
_sharedns_new(Py_ssize_t len)
{
    _sharedns *shared = PyMem_NEW(_sharedns, 1);
    if (shared == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    shared->len = len;
    shared->items = PyMem_NEW(struct _sharednsitem, len);
    if (shared->items == NULL) {
        PyErr_NoMemory();
        PyMem_Free(shared);
        return NULL;
    }
    return shared;
}

static void
_sharedns_free(_sharedns *shared)
{
    for (Py_ssize_t i=0; i < shared->len; i++) {
        _sharednsitem_clear(&shared->items[i]);
    }
    PyMem_Free(shared->items);
    PyMem_Free(shared);
}

static _sharedns *
_get_shared_ns(PyObject *shareable)
{
    if (shareable == NULL || shareable == Py_None) {
        return NULL;
    }
    Py_ssize_t len = PyDict_Size(shareable);
    if (len == 0) {
        return NULL;
    }

    _sharedns *shared = _sharedns_new(len);
    if (shared == NULL) {
        return NULL;
    }
    Py_ssize_t pos = 0;
    for (Py_ssize_t i=0; i < len; i++) {
        PyObject *key, *value;
        if (PyDict_Next(shareable, &pos, &key, &value) == 0) {
            break;
        }
        if (_sharednsitem_init(&shared->items[i], key, value) != 0) {
            break;
        }
    }
    if (PyErr_Occurred()) {
        _sharedns_free(shared);
        return NULL;
    }
    return shared;
}

static int
_sharedns_apply(_sharedns *shared, PyObject *ns)
{
    for (Py_ssize_t i=0; i < shared->len; i++) {
        if (_sharednsitem_apply(&shared->items[i], ns) != 0) {
            return -1;
        }
    }
    return 0;
}

// Ultimately we'd like to preserve enough information about the
// exception and traceback that we could re-constitute (or at least
// simulate, a la traceback.TracebackException), and even chain, a copy
// of the exception in the calling interpreter.

typedef struct _sharedexception {
    const char *name;
    const char *msg;
} _sharedexception;

static const struct _sharedexception no_exception = {
    .name = NULL,
    .msg = NULL,
};

static void
_sharedexception_clear(_sharedexception *exc)
{
    if (exc->name != NULL) {
        PyMem_RawFree((void *)exc->name);
    }
    if (exc->msg != NULL) {
        PyMem_RawFree((void *)exc->msg);
    }
}

static const char *
_sharedexception_bind(PyObject *exc, _sharedexception *sharedexc)
{
    assert(exc != NULL);
    const char *failure = NULL;

    PyObject *nameobj = PyUnicode_FromFormat("%S", Py_TYPE(exc));
    if (nameobj == NULL) {
        failure = "unable to format exception type name";
        goto error;
    }
    sharedexc->name = _copy_raw_string(nameobj);
    Py_DECREF(nameobj);
    if (sharedexc->name == NULL) {
        if (PyErr_ExceptionMatches(PyExc_MemoryError)) {
            failure = "out of memory copying exception type name";
        } else {
            failure = "unable to encode and copy exception type name";
        }
        goto error;
    }

    if (exc != NULL) {
        PyObject *msgobj = PyUnicode_FromFormat("%S", exc);
        if (msgobj == NULL) {
            failure = "unable to format exception message";
            goto error;
        }
        sharedexc->msg = _copy_raw_string(msgobj);
        Py_DECREF(msgobj);
        if (sharedexc->msg == NULL) {
            if (PyErr_ExceptionMatches(PyExc_MemoryError)) {
                failure = "out of memory copying exception message";
            } else {
                failure = "unable to encode and copy exception message";
            }
            goto error;
        }
    }

    return NULL;

error:
    assert(failure != NULL);
    PyErr_Clear();
    _sharedexception_clear(sharedexc);
    *sharedexc = no_exception;
    return failure;
}

static void
_sharedexception_apply(_sharedexception *exc, PyObject *wrapperclass)
{
    if (exc->name != NULL) {
        if (exc->msg != NULL) {
            PyErr_Format(wrapperclass, "%s: %s",  exc->name, exc->msg);
        }
        else {
            PyErr_SetString(wrapperclass, exc->name);
        }
    }
    else if (exc->msg != NULL) {
        PyErr_SetString(wrapperclass, exc->msg);
    }
    else {
        PyErr_SetNone(wrapperclass);
    }
}


/* interpreter-specific code ************************************************/

static int
exceptions_init(PyObject *mod)
{
    module_state *state = get_module_state(mod);
    if (state == NULL) {
        return -1;
    }

#define ADD(NAME, BASE) \
    do { \
        assert(state->NAME == NULL); \
        state->NAME = ADD_NEW_EXCEPTION(mod, NAME, BASE); \
        if (state->NAME == NULL) { \
            return -1; \
        } \
    } while (0)

    // An uncaught exception came out of interp_run_string().
    ADD(RunFailedError, PyExc_RuntimeError);
#undef ADD

    return 0;
}

static int
_is_running(PyInterpreterState *interp)
{
    PyThreadState *tstate = PyInterpreterState_ThreadHead(interp);
    if (PyThreadState_Next(tstate) != NULL) {
        PyErr_SetString(PyExc_RuntimeError,
                        "interpreter has more than one thread");
        return -1;
    }

    assert(!PyErr_Occurred());
    _PyInterpreterFrame *frame = tstate->cframe->current_frame;
    if (frame == NULL) {
        return 0;
    }
    return 1;
}

static int
_ensure_not_running(PyInterpreterState *interp)
{
    int is_running = _is_running(interp);
    if (is_running < 0) {
        return -1;
    }
    if (is_running) {
        PyErr_Format(PyExc_RuntimeError, "interpreter already running");
        return -1;
    }
    return 0;
}

static int
_run_script(PyInterpreterState *interp, const char *codestr,
            _sharedns *shared, _sharedexception *sharedexc)
{
    PyObject *excval = NULL;
    PyObject *main_mod = _PyInterpreterState_GetMainModule(interp);
    if (main_mod == NULL) {
        goto error;
    }
    PyObject *ns = PyModule_GetDict(main_mod);  // borrowed
    Py_DECREF(main_mod);
    if (ns == NULL) {
        goto error;
    }
    Py_INCREF(ns);

    // Apply the cross-interpreter data.
    if (shared != NULL) {
        if (_sharedns_apply(shared, ns) != 0) {
            Py_DECREF(ns);
            goto error;
        }
    }

    // Run the string (see PyRun_SimpleStringFlags).
    PyObject *result = PyRun_StringFlags(codestr, Py_file_input, ns, ns, NULL);
    Py_DECREF(ns);
    if (result == NULL) {
        goto error;
    }
    else {
        Py_DECREF(result);  // We throw away the result.
    }

    *sharedexc = no_exception;
    return 0;

error:
    excval = PyErr_GetRaisedException();
    const char *failure = _sharedexception_bind(excval, sharedexc);
    if (failure != NULL) {
        fprintf(stderr,
                "RunFailedError: script raised an uncaught exception (%s)",
                failure);
        PyErr_Clear();
    }
    Py_XDECREF(excval);
    assert(!PyErr_Occurred());
    return -1;
}

static int
_run_script_in_interpreter(PyObject *mod, PyInterpreterState *interp,
                           const char *codestr, PyObject *shareables)
{
    if (_ensure_not_running(interp) < 0) {
        return -1;
    }
    module_state *state = get_module_state(mod);

    _sharedns *shared = _get_shared_ns(shareables);
    if (shared == NULL && PyErr_Occurred()) {
        return -1;
    }

    // Switch to interpreter.
    PyThreadState *save_tstate = NULL;
    if (interp != PyInterpreterState_Get()) {
        // XXX Using the "head" thread isn't strictly correct.
        PyThreadState *tstate = PyInterpreterState_ThreadHead(interp);
        // XXX Possible GILState issues?
        save_tstate = PyThreadState_Swap(tstate);
    }

    // Run the script.
    _sharedexception exc;
    int result = _run_script(interp, codestr, shared, &exc);

    // Switch back.
    if (save_tstate != NULL) {
        PyThreadState_Swap(save_tstate);
    }

    // Propagate any exception out to the caller.
    if (exc.name != NULL) {
        assert(state != NULL);
        _sharedexception_apply(&exc, state->RunFailedError);
    }
    else if (result != 0) {
        // We were unable to allocate a shared exception.
        PyErr_NoMemory();
    }

    if (shared != NULL) {
        _sharedns_free(shared);
    }

    return result;
}


/* module level code ********************************************************/

static PyObject *
interp_create(PyObject *self, PyObject *args, PyObject *kwds)
{

    static char *kwlist[] = {"isolated", NULL};
    int isolated = 1;
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|$i:create", kwlist,
                                     &isolated)) {
        return NULL;
    }

    // Create and initialize the new interpreter.
    PyThreadState *save_tstate = _PyThreadState_GET();
    const _PyInterpreterConfig config = isolated
        ? (_PyInterpreterConfig)_PyInterpreterConfig_INIT
        : (_PyInterpreterConfig)_PyInterpreterConfig_LEGACY_INIT;
    // XXX Possible GILState issues?
    PyThreadState *tstate = _Py_NewInterpreterFromConfig(&config);
    PyThreadState_Swap(save_tstate);
    if (tstate == NULL) {
        /* Since no new thread state was created, there is no exception to
           propagate; raise a fresh one after swapping in the old thread
           state. */
        PyErr_SetString(PyExc_RuntimeError, "interpreter creation failed");
        return NULL;
    }
    PyInterpreterState *interp = PyThreadState_GetInterpreter(tstate);
    PyObject *idobj = _PyInterpreterState_GetIDObject(interp);
    if (idobj == NULL) {
        // XXX Possible GILState issues?
        save_tstate = PyThreadState_Swap(tstate);
        Py_EndInterpreter(tstate);
        PyThreadState_Swap(save_tstate);
        return NULL;
    }
    _PyInterpreterState_RequireIDRef(interp, 1);
    return idobj;
}

PyDoc_STRVAR(create_doc,
"create() -> ID\n\
\n\
Create a new interpreter and return a unique generated ID.");


static PyObject *
interp_destroy(PyObject *self, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = {"id", NULL};
    PyObject *id;
    // XXX Use "L" for id?
    if (!PyArg_ParseTupleAndKeywords(args, kwds,
                                     "O:destroy", kwlist, &id)) {
        return NULL;
    }

    // Look up the interpreter.
    PyInterpreterState *interp = _PyInterpreterID_LookUp(id);
    if (interp == NULL) {
        return NULL;
    }

    // Ensure we don't try to destroy the current interpreter.
    PyInterpreterState *current = _get_current_interp();
    if (current == NULL) {
        return NULL;
    }
    if (interp == current) {
        PyErr_SetString(PyExc_RuntimeError,
                        "cannot destroy the current interpreter");
        return NULL;
    }

    // Ensure the interpreter isn't running.
    /* XXX We *could* support destroying a running interpreter but
       aren't going to worry about it for now. */
    if (_ensure_not_running(interp) < 0) {
        return NULL;
    }

    // Destroy the interpreter.
    PyThreadState *tstate = PyInterpreterState_ThreadHead(interp);
    // XXX Possible GILState issues?
    PyThreadState *save_tstate = PyThreadState_Swap(tstate);
    Py_EndInterpreter(tstate);
    PyThreadState_Swap(save_tstate);

    Py_RETURN_NONE;
}

PyDoc_STRVAR(destroy_doc,
"destroy(id)\n\
\n\
Destroy the identified interpreter.\n\
\n\
Attempting to destroy the current interpreter results in a RuntimeError.\n\
So does an unrecognized ID.");


static PyObject *
interp_list_all(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *ids, *id;
    PyInterpreterState *interp;

    ids = PyList_New(0);
    if (ids == NULL) {
        return NULL;
    }

    interp = PyInterpreterState_Head();
    while (interp != NULL) {
        id = _PyInterpreterState_GetIDObject(interp);
        if (id == NULL) {
            Py_DECREF(ids);
            return NULL;
        }
        // insert at front of list
        int res = PyList_Insert(ids, 0, id);
        Py_DECREF(id);
        if (res < 0) {
            Py_DECREF(ids);
            return NULL;
        }

        interp = PyInterpreterState_Next(interp);
    }

    return ids;
}

PyDoc_STRVAR(list_all_doc,
"list_all() -> [ID]\n\
\n\
Return a list containing the ID of every existing interpreter.");


static PyObject *
interp_get_current(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    PyInterpreterState *interp =_get_current_interp();
    if (interp == NULL) {
        return NULL;
    }
    return _PyInterpreterState_GetIDObject(interp);
}

PyDoc_STRVAR(get_current_doc,
"get_current() -> ID\n\
\n\
Return the ID of current interpreter.");


static PyObject *
interp_get_main(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    // Currently, 0 is always the main interpreter.
    int64_t id = 0;
    return _PyInterpreterID_New(id);
}

PyDoc_STRVAR(get_main_doc,
"get_main() -> ID\n\
\n\
Return the ID of main interpreter.");


static PyObject *
interp_run_string(PyObject *self, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = {"id", "script", "shared", NULL};
    PyObject *id, *code;
    PyObject *shared = NULL;
    if (!PyArg_ParseTupleAndKeywords(args, kwds,
                                     "OU|O:run_string", kwlist,
                                     &id, &code, &shared)) {
        return NULL;
    }

    // Look up the interpreter.
    PyInterpreterState *interp = _PyInterpreterID_LookUp(id);
    if (interp == NULL) {
        return NULL;
    }

    // Extract code.
    Py_ssize_t size;
    const char *codestr = PyUnicode_AsUTF8AndSize(code, &size);
    if (codestr == NULL) {
        return NULL;
    }
    if (strlen(codestr) != (size_t)size) {
        PyErr_SetString(PyExc_ValueError,
                        "source code string cannot contain null bytes");
        return NULL;
    }

    // Run the code in the interpreter.
    if (_run_script_in_interpreter(self, interp, codestr, shared) != 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

PyDoc_STRVAR(run_string_doc,
"run_string(id, script, shared)\n\
\n\
Execute the provided string in the identified interpreter.\n\
\n\
See PyRun_SimpleStrings.");


static PyObject *
object_is_shareable(PyObject *self, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = {"obj", NULL};
    PyObject *obj;
    if (!PyArg_ParseTupleAndKeywords(args, kwds,
                                     "O:is_shareable", kwlist, &obj)) {
        return NULL;
    }

    if (_PyObject_CheckCrossInterpreterData(obj) == 0) {
        Py_RETURN_TRUE;
    }
    PyErr_Clear();
    Py_RETURN_FALSE;
}

PyDoc_STRVAR(is_shareable_doc,
"is_shareable(obj) -> bool\n\
\n\
Return True if the object's data may be shared between interpreters and\n\
False otherwise.");


static PyObject *
interp_is_running(PyObject *self, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = {"id", NULL};
    PyObject *id;
    if (!PyArg_ParseTupleAndKeywords(args, kwds,
                                     "O:is_running", kwlist, &id)) {
        return NULL;
    }

    PyInterpreterState *interp = _PyInterpreterID_LookUp(id);
    if (interp == NULL) {
        return NULL;
    }
    int is_running = _is_running(interp);
    if (is_running < 0) {
        return NULL;
    }
    if (is_running) {
        Py_RETURN_TRUE;
    }
    Py_RETURN_FALSE;
}

PyDoc_STRVAR(is_running_doc,
"is_running(id) -> bool\n\
\n\
Return whether or not the identified interpreter is running.");

static PyMethodDef module_functions[] = {
    {"create",                    _PyCFunction_CAST(interp_create),
     METH_VARARGS | METH_KEYWORDS, create_doc},
    {"destroy",                   _PyCFunction_CAST(interp_destroy),
     METH_VARARGS | METH_KEYWORDS, destroy_doc},
    {"list_all",                  interp_list_all,
     METH_NOARGS, list_all_doc},
    {"get_current",               interp_get_current,
     METH_NOARGS, get_current_doc},
    {"get_main",                  interp_get_main,
     METH_NOARGS, get_main_doc},

    {"is_running",                _PyCFunction_CAST(interp_is_running),
     METH_VARARGS | METH_KEYWORDS, is_running_doc},
    {"run_string",                _PyCFunction_CAST(interp_run_string),
     METH_VARARGS | METH_KEYWORDS, run_string_doc},

    {"is_shareable",              _PyCFunction_CAST(object_is_shareable),
     METH_VARARGS | METH_KEYWORDS, is_shareable_doc},

    {NULL,                        NULL}           /* sentinel */
};


/* initialization function */

PyDoc_STRVAR(module_doc,
"This module provides primitive operations to manage Python interpreters.\n\
The 'interpreters' module provides a more convenient interface.");

static int
module_exec(PyObject *mod)
{
    /* Add exception types */
    if (exceptions_init(mod) != 0) {
        goto error;
    }

    // PyInterpreterID
    if (PyModule_AddType(mod, &_PyInterpreterID_Type) < 0) {
        goto error;
    }

    return 0;

error:
    return -1;
}

static struct PyModuleDef_Slot module_slots[] = {
    {Py_mod_exec, module_exec},
    {0, NULL},
};

static int
module_traverse(PyObject *mod, visitproc visit, void *arg)
{
    module_state *state = get_module_state(mod);
    assert(state != NULL);
    traverse_module_state(state, visit, arg);
    return 0;
}

static int
module_clear(PyObject *mod)
{
    module_state *state = get_module_state(mod);
    assert(state != NULL);
    clear_module_state(state);
    return 0;
}

static void
module_free(void *mod)
{
    module_state *state = get_module_state(mod);
    assert(state != NULL);
    clear_module_state(state);
}

static struct PyModuleDef moduledef = {
    .m_base = PyModuleDef_HEAD_INIT,
    .m_name = MODULE_NAME,
    .m_doc = module_doc,
    .m_size = sizeof(module_state),
    .m_methods = module_functions,
    .m_slots = module_slots,
    .m_traverse = module_traverse,
    .m_clear = module_clear,
    .m_free = (freefunc)module_free,
};

PyMODINIT_FUNC
PyInit__xxsubinterpreters(void)
{
    return PyModuleDef_Init(&moduledef);
}
