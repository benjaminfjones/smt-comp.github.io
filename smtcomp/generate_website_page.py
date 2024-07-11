import functools, itertools
from typing import Set, Dict, Optional, cast, List, DefaultDict
from pathlib import Path, PurePath
from smtcomp import defs
from rich import progress
from rich import print
from pydantic import BaseModel
import polars as pl
import smtcomp.scoring
from smtcomp.utils import *
import smtcomp.results

# Warning: Hugo lowercase all dict keys


class PodiumStep(BaseModel):
    name: str
    competing: str  # yes or no
    errorScore: int
    correctScore: int
    CPUScore: float
    WallScore: float
    solved: int
    solved_sat: int
    solved_unsat: int
    unsolved: int
    abstained: int
    timeout: int
    memout: int


class PodiumDivision(BaseModel):
    resultdate: str
    year: int
    divisions: str  # divisions_2023
    participants: str  # participants_2023
    disagreements: str  # disagreements_2023
    division: str  # Arith
    track: str  # track_single_query
    n_benchmarks: int
    time_limit: int
    mem_limit: int
    logics: dict[str, int]
    winner_seq: str
    winner_par: str
    winner_sat: str
    winner_unsat: str
    winner_24s: str

    sequential: list[PodiumStep]
    parallel: list[PodiumStep]
    sat: list[PodiumStep]
    unsat: list[PodiumStep]
    twentyfour: list[PodiumStep]

    layout: str = "result"


def podium_steps(podium: List[dict[str, Any]] | None) -> List[PodiumStep]:
    if podium is None:
        return []
    else:
        return [
            PodiumStep(
                name=s["solver"],
                competing="yes",  # TODO
                errorScore=-s["error_score"],
                correctScore=s["correctly_solved_score"],
                CPUScore=s["cpu_time_score"],
                WallScore=s["wallclock_time_score"],
                solved=s["solved"],
                solved_sat=s["solved_sat"],
                solved_unsat=s["solved_unsat"],
                unsolved=s["unsolved"],
                abstained=s["abstained"],
                timeout=s["timeout"],
                memout=s["memout"],
            )
            for s in podium
        ]


def make_podium(config: defs.Config, d: dict[str, Any], for_division: bool) -> PodiumDivision:
    def get_winner(l: List[dict[str, str]] | None) -> str:
        # TODO select only participating
        if l is None or l[0]["correctly_solved_score"] == 0:
            return "-"
        else:
            return l[0]["solver"]

    if for_division:
        division = defs.Division.name_of_int(d["division"])
        logics = dict((defs.Logic.name_of_int(d2["logic"]), d2["n"]) for d2 in d["logics"])
    else:
        division = defs.Logic.name_of_int(d["logic"])
        logics = dict()

    return PodiumDivision(
        resultdate="2024-07-08",
        year=config.current_year,
        divisions=f"divisions_{config.current_year}",
        participants=f"participants_{config.current_year}",
        disagreements=f"disagreements_{config.current_year}",
        division=division,
        track="track_single_query",
        n_benchmarks=d["total"],
        time_limit=config.timelimit_s,
        mem_limit=config.memlimit_M,
        logics=dict(sorted(logics.items())),
        winner_seq=get_winner(d[smtcomp.scoring.Kind.seq.name]),
        winner_par=get_winner(d[smtcomp.scoring.Kind.par.name]),
        winner_sat=get_winner(d[smtcomp.scoring.Kind.sat.name]),
        winner_unsat=get_winner(d[smtcomp.scoring.Kind.unsat.name]),
        winner_24s=get_winner(d[smtcomp.scoring.Kind.twentyfour.name]),
        sequential=podium_steps(d[smtcomp.scoring.Kind.seq.name]),
        parallel=podium_steps(d[smtcomp.scoring.Kind.par.name]),
        sat=podium_steps(d[smtcomp.scoring.Kind.sat.name]),
        unsat=podium_steps(d[smtcomp.scoring.Kind.unsat.name]),
        twentyfour=podium_steps(d[smtcomp.scoring.Kind.twentyfour.name]),
    )


def sq_generate_datas(
    config: defs.Config, selection: pl.LazyFrame, results: pl.LazyFrame, for_division: bool
) -> dict[str, PodiumDivision]:
    """
    Generate datas for divisions or for logics
    """

    if for_division:
        group_by = "division"
        name_of_int = defs.Division.name_of_int
    else:
        group_by = "logic"
        name_of_int = defs.Logic.name_of_int

    results = results.filter(track=int(defs.Track.SingleQuery)).drop("track")

    selection = selection.filter(selected=True)

    len_by_division = selection.group_by(group_by).agg(total=pl.len())

    def info_for_podium_step(kind: smtcomp.scoring.Kind, config: defs.Config, results: pl.LazyFrame) -> pl.LazyFrame:
        results = smtcomp.scoring.filter_for(kind, config, results)
        return (
            intersect(results, len_by_division, on=[group_by])
            .group_by(group_by, "solver")
            .agg(
                pl.sum("error_score"),
                pl.sum("correctly_solved_score"),
                pl.sum("cpu_time_score"),
                pl.sum("wallclock_time_score"),
                solved=(smtcomp.scoring.known_answer).sum(),
                solved_sat=(smtcomp.scoring.sat_answer).sum(),
                solved_unsat=(smtcomp.scoring.unsat_answer).sum(),
                unsolved=(smtcomp.scoring.unknown_answer).sum(),
                timeout=(smtcomp.scoring.timeout_answer).sum(),
                memout=(smtcomp.scoring.memout_answer).sum(),
                abstained=pl.col("total").first() - pl.len(),
            )
            .sort([group_by] + smtcomp.scoring.scores + ["solver"], descending=True)
            .group_by(group_by, maintain_order=True)
            .agg(
                pl.struct(
                    "solver",
                    "error_score",
                    "correctly_solved_score",
                    "cpu_time_score",
                    "wallclock_time_score",
                    "solved",
                    "solved_sat",
                    "solved_unsat",
                    "unsolved",
                    "timeout",
                    "memout",
                    "abstained",
                ).alias(kind.name)
            )
        )

    if for_division:
        lf_logics = [
            selection.group_by("division", "logic")
            .agg(n=pl.len())
            .group_by("division")
            .agg(logics=pl.struct("logic", "n"))
        ]
    else:
        lf_logics = []

    l = [len_by_division] + lf_logics + [info_for_podium_step(kind, config, results) for kind in smtcomp.scoring.Kind]

    r = functools.reduce(lambda x, y: x.join(y, validate="1:1", on=[group_by], how="left"), l)

    df = r.collect()

    return dict((name_of_int(d[group_by]), make_podium(config, d, for_division)) for d in df.to_dicts())


def export_results(config: defs.Config, selection: pl.LazyFrame, results: pl.LazyFrame) -> None:

    dst = config.web_results
    dst.mkdir(parents=True, exist_ok=True)

    results = results.filter(disagreements=False).drop("disagreements")
    results = results.collect().lazy()

    for for_division in [True, False]:
        for name, data in sq_generate_datas(config, selection, results, for_division).items():
            (dst / f"{name.lower()}-single-query.md").write_text(data.model_dump_json(indent=1))
