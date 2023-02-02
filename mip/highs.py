"Python-MIP interface to the HiGHS solver."

import glob
import numbers
import logging
import os
import os.path
import sys
from typing import List, Optional, Tuple, Union

import cffi

import mip

logger = logging.getLogger(__name__)

# try loading the solver library
ffi = cffi.FFI()
try:
    # first try user-defined path, if given
    ENV_KEY = "PMIP_HIGHS_LIBRARY"
    if ENV_KEY in os.environ:
        libfile = os.environ[ENV_KEY]
        logger.debug("Choosing HiGHS library {libfile} via {ENV_KEY}.")
    else:
        # try library shipped with highspy packaged
        import highspy

        pkg_path = os.path.dirname(highspy.__file__)

        # need library matching operating system
        if "linux" in sys.platform.lower():
            pattern = "highs_bindings.*.so"
        else:
            raise NotImplementedError(f"{sys.platform} not supported!")

        # there should only be one match
        [libfile] = glob.glob(os.path.join(pkg_path, pattern))
        logger.debug("Choosing HiGHS library {libfile} via highspy package.")

    highslib = ffi.dlopen(libfile)
    has_highs = True
except Exception as e:
    logger.error(f"An error occurred while loading the HiGHS library:\n{e}")
    has_highs = False

HEADER = """
typedef int HighsInt;

const HighsInt kHighsObjSenseMinimize = 1;
const HighsInt kHighsObjSenseMaximize = -1;

const HighsInt kHighsVarTypeContinuous = 0;
const HighsInt kHighsVarTypeInteger = 1;

void* Highs_create(void);
void Highs_destroy(void* highs);
HighsInt Highs_readModel(void* highs, const char* filename);
HighsInt Highs_writeModel(void* highs, const char* filename);
HighsInt Highs_run(void* highs);
HighsInt Highs_getModelStatus(const void* highs);
double Highs_getObjectiveValue(const void* highs);
HighsInt Highs_addVar(void* highs, const double lower, const double upper);
HighsInt Highs_addRow(
    void* highs, const double lower, const double upper, const HighsInt num_new_nz,
    const HighsInt* index, const double* value
);
HighsInt Highs_changeObjectiveOffset(void* highs, const double offset);
HighsInt Highs_changeObjectiveSense(void* highs, const HighsInt sense);
HighsInt Highs_changeColIntegrality(
    void* highs, const HighsInt col, const HighsInt integrality
);
HighsInt Highs_changeColsIntegralityByRange(
    void* highs, const HighsInt from_col, const HighsInt to_col,
    const HighsInt* integrality
);
HighsInt Highs_changeColCost(void* highs, const HighsInt col, const double cost);
HighsInt Highs_changeColBounds(
    void* highs, const HighsInt col, const double lower, const double upper
);
HighsInt Highs_getRowsByRange(
    const void* highs, const HighsInt from_row, const HighsInt to_row,
    HighsInt* num_row, double* lower, double* upper, HighsInt* num_nz,
    HighsInt* matrix_start, HighsInt* matrix_index, double* matrix_value
);
HighsInt Highs_getColsByRange(
    const void* highs, const HighsInt from_col, const HighsInt to_col,
    HighsInt* num_col, double* costs, double* lower, double* upper,
    HighsInt* num_nz, HighsInt* matrix_start, HighsInt* matrix_index,
    double* matrix_value
);
HighsInt Highs_getObjectiveOffset(const void* highs, double* offset);
HighsInt Highs_getObjectiveSense(const void* highs, HighsInt* sense);
HighsInt Highs_getNumCol(const void* highs);
HighsInt Highs_getNumRow(const void* highs);
HighsInt Highs_getDoubleInfoValue(
    const void* highs, const char* info, double* value
);
HighsInt Highs_getIntInfoValue(
    const void* highs, const char* info, int* value
);
"""

if has_highs:
    ffi.cdef(HEADER)


class SolverHighs(mip.Solver):
    def __init__(self, model: mip.Model, name: str, sense: str):
        if not has_highs:
            raise FileNotFoundError(
                "HiGHS not found."
                "Please install the `highspy` package, or"
                "set the `PMIP_HIGHS_LIBRARY` environment variable."
            )

        # Store reference to library so that it's not garbage-collected (when we
        # just use highslib in __del__, it had already become None)?!
        self._lib = highslib

        super().__init__(model, name, sense)

        # Model creation and initialization.
        self._model = highslib.Highs_create()
        self.set_objective_sense(sense)

        # Store additional data here, if HiGHS can't do it.
        self._name = name
        self._var_name: List[str] = []
        self._var_col: Dict[str, int] = {}
        self._cons_name: List[str] = []
        self._cons_col: Dict[str, int] = {}

    def __del__(self):
        self._lib.Highs_destroy(self._model)

    def _get_int_info_value(self: "SolverHighs", name: str) -> int:
        value = ffi.new("int*")
        status = self._lib.Highs_getIntInfoValue(
            self._model, name.encode("UTF-8"), value
        )
        return value[0]

    def _get_double_info_value(self: "SolverHighs", name: str) -> float:
        value = ffi.new("double*")
        status = self._lib.Highs_getDoubleInfoValue(
            self._model, name.encode("UTF-8"), value
        )
        return value[0]

    def add_var(
        self: "SolverHighs",
        obj: numbers.Real = 0,
        lb: numbers.Real = 0,
        ub: numbers.Real = mip.INF,
        var_type: str = mip.CONTINUOUS,
        column: "Column" = None,
        name: str = "",
    ):
        # TODO: handle column data
        col: int = self.num_cols()
        # TODO: handle status (everywhere)
        status = self._lib.Highs_addVar(self._model, lb, ub)
        status = self._lib.Highs_changeColCost(self._model, col, obj)
        if var_type != mip.CONTINUOUS:
            status = self._lib.Highs_changeColIntegrality(
                self._model, col, self._lib.kHighsVarTypeInteger
            )

        # store name
        self._var_name.append(name)
        self._var_col[name] = col

    def add_constr(self: "SolverHighs", lin_expr: "mip.LinExpr", name: str = ""):
        row: int = self.num_rows()

        # equation expressed as two-sided inequality
        lower = -lin_expr.const
        upper = -lin_expr.const
        if lin_expr.sense == mip.LESS_OR_EQUAL:
            lower = -mip.INF
        elif lin_expr.sense == mip.GREATER_OR_EQUAL:
            upper = mip.INF
        else:
            assert lin_expr.sense == mip.EQUAL

        num_new_nz = len(lin_expr.expr)
        index = ffi.new("int[]", [var.idx for var in lin_expr.expr.keys()])
        value = ffi.new("double[]", [coef for coef in lin_expr.expr.values()])

        status = self._lib.Highs_addRow(
            self._model, lower, upper, num_new_nz, index, value
        )

        # store name
        self._cons_name.append(name)
        self._cons_col[name] = row

    def add_lazy_constr(self: "SolverHighs", lin_expr: "mip.LinExpr"):
        raise NotImplementedError()

    def add_sos(
        self: "SolverHighs",
        sos: List[Tuple["mip.Var", numbers.Real]],
        sos_type: int,
    ):
        raise NotImplementedError()

    def add_cut(self: "SolverHighs", lin_expr: "mip.LinExpr"):
        raise NotImplementedError()

    def get_objective_bound(self: "SolverHighs") -> numbers.Real:
        return self._get_double_info_value("mip_dual_bound")

    def get_objective(self: "SolverHighs") -> "mip.LinExpr":
        n = self.num_cols()
        num_col = ffi.new("int*")
        costs = ffi.new("double[]", n)
        lower = ffi.new("double[]", n)
        upper = ffi.new("double[]", n)
        num_nz = ffi.new("int*")
        status = self._lib.Highs_getColsByRange(
            self._model,
            0,  # from_col
            n - 1,  # to_col
            num_col,
            costs,
            lower,
            upper,
            num_nz,
            ffi.NULL,  # matrix_start
            ffi.NULL,  # matrix_index
            ffi.NULL,  # matrix_value
        )
        obj_expr = mip.xsum(
            costs[i] * self.model.vars[i] for i in range(n) if costs[i] != 0.0
        )
        obj_expr.add_const(self.get_objective_const())
        obj_expr.sense = self.get_objective_sense()
        return obj_expr

    def get_objective_const(self: "SolverHighs") -> numbers.Real:
        offset = ffi.new("double*")
        status = self._lib.Highs_getObjectiveOffset(self._model, offset)
        return offset[0]

    def relax(self: "SolverHighs"):
        # change integrality of all columns
        n = self.num_cols()
        integrality = ffi.new(
            "int[]", [self._lib.kHighsVarTypeContinuous for i in range(n)]
        )
        status = self._lib.Highs_changeColsIntegralityByRange(
            self._model, 0, n - 1, integrality
        )

    def generate_cuts(
        self,
        cut_types: Optional[List[mip.CutType]] = None,
        depth: int = 0,
        npass: int = 0,
        max_cuts: int = mip.INT_MAX,
        min_viol: numbers.Real = 1e-4,
    ) -> "mip.CutPool":
        raise NotImplementedError()

    def clique_merge(self, constrs: Optional[List["mip.Constr"]] = None):
        raise NotImplementedError()

    def optimize(
        self: "SolverHighs",
        relax: bool = False,
    ) -> "mip.OptimizationStatus":
        pass

    def get_objective_value(self: "SolverHighs") -> numbers.Real:
        pass

    def get_log(
        self: "SolverHighs",
    ) -> List[Tuple[numbers.Real, Tuple[numbers.Real, numbers.Real]]]:
        raise NotImplementedError()

    def get_objective_value_i(self: "SolverHighs", i: int) -> numbers.Real:
        raise NotImplementedError()

    def get_num_solutions(self: "SolverHighs") -> int:
        pass

    def get_objective_sense(self: "SolverHighs") -> str:
        sense = ffi.new("int*")
        status = self._lib.Highs_getObjectiveSense(self._model, sense)
        sense_map = {
            self._lib.kHighsObjSenseMaximize: mip.MAXIMIZE,
            self._lib.kHighsObjSenseMinimize: mip.MINIMIZE,
        }
        return sense_map[sense[0]]

    def set_objective_sense(self: "SolverHighs", sense: str):
        sense_map = {
            mip.MAXIMIZE: self._lib.kHighsObjSenseMaximize,
            mip.MINIMIZE: self._lib.kHighsObjSenseMinimize,
        }
        status = self._lib.Highs_changeObjectiveSense(self._model, sense_map[sense])

    def set_start(self: "SolverHighs", start: List[Tuple["mip.Var", numbers.Real]]):
        raise NotImplementedError()

    def set_objective(self: "SolverHighs", lin_expr: "mip.LinExpr", sense: str = ""):
        # set coefficients
        for var, coef in lin_expr.expr.items():
            status = self._lib.Highs_changeColCost(self._model, var.idx, coef)

        self.set_objective_const(lin_expr.const)
        self.set_objective_sense(lin_expr.sense)

    def set_objective_const(self: "SolverHighs", const: numbers.Real):
        status = self._lib.Highs_changeObjectiveOffset(self._model, const)

    def set_processing_limits(
        self: "SolverHighs",
        max_time: numbers.Real = mip.INF,
        max_nodes: int = mip.INT_MAX,
        max_sol: int = mip.INT_MAX,
        max_seconds_same_incumbent: float = mip.INF,
        max_nodes_same_incumbent: int = mip.INT_MAX,
    ):
        pass

    def get_max_seconds(self: "SolverHighs") -> numbers.Real:
        pass

    def set_max_seconds(self: "SolverHighs", max_seconds: numbers.Real):
        pass

    def get_max_solutions(self: "SolverHighs") -> int:
        pass

    def set_max_solutions(self: "SolverHighs", max_solutions: int):
        pass

    def get_pump_passes(self: "SolverHighs") -> int:
        raise NotImplementedError()

    def set_pump_passes(self: "SolverHighs", passes: int):
        raise NotImplementedError()

    def get_max_nodes(self: "SolverHighs") -> int:
        pass

    def set_max_nodes(self: "SolverHighs", max_nodes: int):
        pass

    def set_num_threads(self: "SolverHighs", threads: int):
        pass

    def write(self: "SolverHighs", file_path: str):
        pass

    def read(self: "SolverHighs", file_path: str):
        pass

    def num_cols(self: "SolverHighs") -> int:
        return self._lib.Highs_getNumCol(self._model)

    def num_rows(self: "SolverHighs") -> int:
        return self._lib.Highs_getNumRow(self._model)

    def num_nz(self: "SolverHighs") -> int:
        pass

    def num_int(self: "SolverHighs") -> int:
        pass

    def get_emphasis(self: "SolverHighs") -> mip.SearchEmphasis:
        pass

    def set_emphasis(self: "SolverHighs", emph: mip.SearchEmphasis):
        pass

    def get_cutoff(self: "SolverHighs") -> numbers.Real:
        pass

    def set_cutoff(self: "SolverHighs", cutoff: numbers.Real):
        pass

    def get_mip_gap_abs(self: "SolverHighs") -> numbers.Real:
        pass

    def set_mip_gap_abs(self: "SolverHighs", mip_gap_abs: numbers.Real):
        pass

    def get_mip_gap(self: "SolverHighs") -> numbers.Real:
        pass

    def set_mip_gap(self: "SolverHighs", mip_gap: numbers.Real):
        pass

    def get_verbose(self: "SolverHighs") -> int:
        pass

    def set_verbose(self: "SolverHighs", verbose: int):
        pass

    # Constraint-related getters/setters

    def constr_get_expr(self: "SolverHighs", constr: "mip.Constr") -> "mip.LinExpr":
        pass

    def constr_set_expr(
        self: "SolverHighs", constr: "mip.Constr", value: "mip.LinExpr"
    ) -> "mip.LinExpr":
        pass

    def constr_get_rhs(self: "SolverHighs", idx: int) -> numbers.Real:
        pass

    def constr_set_rhs(self: "SolverHighs", idx: int, rhs: numbers.Real):
        pass

    def constr_get_name(self: "SolverHighs", idx: int) -> str:
        pass

    def constr_get_pi(self: "SolverHighs", constr: "mip.Constr") -> numbers.Real:
        pass

    def constr_get_slack(self: "SolverHighs", constr: "mip.Constr") -> numbers.Real:
        pass

    def remove_constrs(self: "SolverHighs", constrsList: List[int]):
        pass

    def constr_get_index(self: "SolverHighs", name: str) -> int:
        pass

    # Variable-related getters/setters

    def var_get_branch_priority(self: "SolverHighs", var: "mip.Var") -> numbers.Real:
        raise NotImplementedError()

    def var_set_branch_priority(
        self: "SolverHighs", var: "mip.Var", value: numbers.Real
    ):
        raise NotImplementedError()

    def var_get_lb(self: "SolverHighs", var: "mip.Var") -> numbers.Real:
        pass

    def var_set_lb(self: "SolverHighs", var: "mip.Var", value: numbers.Real):
        pass

    def var_get_ub(self: "SolverHighs", var: "mip.Var") -> numbers.Real:
        pass

    def var_set_ub(self: "SolverHighs", var: "mip.Var", value: numbers.Real):
        pass

    def var_get_obj(self: "SolverHighs", var: "mip.Var") -> numbers.Real:
        pass

    def var_set_obj(self: "SolverHighs", var: "mip.Var", value: numbers.Real):
        pass

    def var_get_var_type(self: "SolverHighs", var: "mip.Var") -> str:
        pass

    def var_set_var_type(self: "SolverHighs", var: "mip.Var", value: str):
        pass

    def var_get_column(self: "SolverHighs", var: "mip.Var") -> "Column":
        pass

    def var_set_column(self: "SolverHighs", var: "mip.Var", value: "Column"):
        pass

    def var_get_rc(self: "SolverHighs", var: "mip.Var") -> numbers.Real:
        pass

    def var_get_x(self: "SolverHighs", var: "mip.Var") -> numbers.Real:
        pass

    def var_get_xi(self: "SolverHighs", var: "mip.Var", i: int) -> numbers.Real:
        pass

    def var_get_name(self: "SolverHighs", idx: int) -> str:
        return self._var_name[idx]

    def remove_vars(self: "SolverHighs", varsList: List[int]):
        pass

    def var_get_index(self: "SolverHighs", name: str) -> int:
        return self._var_col[name]

    def get_problem_name(self: "SolverHighs") -> str:
        return self._name

    def set_problem_name(self: "SolverHighs", name: str):
        self._name = name

    def get_status(self: "SolverHighs") -> mip.OptimizationStatus:
        pass

    def cgraph_density(self: "SolverHighs") -> float:
        """Density of the conflict graph"""
        raise NotImplementedError()

    def conflicting(
        self: "SolverHighs",
        e1: Union["mip.LinExpr", "mip.Var"],
        e2: Union["mip.LinExpr", "mip.Var"],
    ) -> bool:
        """Checks if two assignment to binary variables are in conflict,
        returns none if no conflict graph is available"""
        raise NotImplementedError()

    def conflicting_nodes(
        self: "SolverHighs", v1: Union["mip.Var", "mip.LinExpr"]
    ) -> Tuple[List["mip.Var"], List["mip.Var"]]:
        """Returns all assignment conflicting with the assignment in v1 in the
        conflict graph.
        """
        raise NotImplementedError()

    def feature_values(self: "SolverHighs") -> List[float]:
        raise NotImplementedError()

    def feature_names(self: "SolverHighs") -> List[str]:
        raise NotImplementedError()
