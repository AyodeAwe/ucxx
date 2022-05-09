/**
 * Copyright (c) 2022, NVIDIA CORPORATION. All rights reserved.
 *
 * See file LICENSE for terms.
 */
#pragma once

#include <exception>

#include "Python.h"

#include <ucp/api/ucp.h>

#include <ucxx/exception.h>

extern "C" {
extern PyObject* ucxx_error;
extern PyObject* ucxx_canceled_error;
extern PyObject* ucxx_config_error;
extern PyObject* ucxx_connection_reset_error;
}

namespace ucxx {

void raise_py_error()
{
  try {
    throw;
  } catch (const UCXXCanceledError& e) {
    PyErr_SetString(ucxx_canceled_error, e.what());
  } catch (const UCXXConfigError& e) {
    PyErr_SetString(ucxx_config_error, e.what());
  } catch (const UCXXConnectionResetError& e) {
    PyErr_SetString(ucxx_connection_reset_error, e.what());
  } catch (const UCXXError& e) {
    PyErr_SetString(ucxx_error, e.what());
  } catch (const std::bad_alloc& e) {
    PyErr_SetString(PyExc_MemoryError, e.what());
  } catch (const std::bad_cast& e) {
    PyErr_SetString(PyExc_TypeError, e.what());
  } catch (const std::bad_typeid& e) {
    PyErr_SetString(PyExc_TypeError, e.what());
  } catch (const std::domain_error& e) {
    PyErr_SetString(PyExc_ValueError, e.what());
  } catch (const std::invalid_argument& e) {
    PyErr_SetString(PyExc_ValueError, e.what());
  } catch (const std::ios_base::failure& e) {
    PyErr_SetString(PyExc_IOError, e.what());
  } catch (const std::out_of_range& e) {
    PyErr_SetString(PyExc_IndexError, e.what());
  } catch (const std::overflow_error& e) {
    PyErr_SetString(PyExc_OverflowError, e.what());
  } catch (const std::range_error& e) {
    PyErr_SetString(PyExc_ArithmeticError, e.what());
  } catch (const std::underflow_error& e) {
    PyErr_SetString(PyExc_ArithmeticError, e.what());
  } catch (const std::exception& e) {
    PyErr_SetString(PyExc_RuntimeError, e.what());
  } catch (...) {
    PyErr_SetString(PyExc_RuntimeError, "Unknown exception");
  }
}

PyObject* get_python_exception_from_ucs_status(ucs_status_t status)
{
  switch (status) {
    case UCS_ERR_CANCELED: return ucxx_canceled_error;
    case UCS_ERR_CONNECTION_RESET: return ucxx_connection_reset_error;
    default: return ucxx_error;
  }
}

}  // namespace ucxx