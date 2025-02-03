#!/usr/bin/env python3

import json
import sys
from pathlib import Path
from typing import Annotated, Any, Literal, Sequence

import chromadb
import tiktoken
import typer
from chromadb.api import ClientAPI
from loguru import logger
from openai import BadRequestError, LengthFinishReasonError
from pydantic import BaseModel

from .clients import openai_client
from .code_quoting import format_file
from .file_filters import get_rel_paths
from .properties import extract_propositions
from .python_deps import get_deps_rec


# Basically we have two methods to control the API usage:
# * max tokens: approximate because we don't know exactly
#   how many tokens we use
# * how much work is done (also approximate)
#   * limited set of properties
#   * max messages per property
#   * max files per message
#   * max file size

MAX_FILES_REQUESTED = 5
# more tokens get sent than we count so this is supposed to be
# an upper bound on the extra (just a guess)
MESSAGE_ENVELOPE_TOKENS = 300

# o1-preview and o1 gave good results. everything else was bad (deepseek,
# claude, gpt-4o, gemini, o3-mini).
MODEL: dict[str, Any] = {
    "model": "o1",
    "reasoning_effort": "medium",
}
# I know it's supposed to be 200k but sometimes I got 400s saying it
# can only do 100k.
MAX_CONTEXT_LENGTH = 100_000

# https://github.com/openai/tiktoken/issues/337#issuecomment-2392465999
enc = tiktoken.encoding_for_model("gpt-4o")

logger.remove()
logger.add(sink=sys.stderr, level="DEBUG")


class PropertyLocation(BaseModel):
    rel_path: Path
    line_num: int


class CorrectnessExplanation(BaseModel):
    correctness: Literal["correct", "incorrect", "unknown"]
    explanation: str


class FilesRequested(BaseModel):
    files_requested: list[str]


class FullFilesExcludedResponse(BaseModel):
    data: FilesRequested | CorrectnessExplanation


class FullFilesIncludedResponse(BaseModel):
    data: CorrectnessExplanation


class Failure(BaseModel):
    msg: str


class ProofCheckResult(BaseModel):
    property_location: PropertyLocation
    correctness_explanation: CorrectnessExplanation | Failure


class Linter:
    def __init__(
        self,
        directory: Path,
        max_messages: int,
        min_length_to_exclude_full_files: int,
        max_files: int,
        filter_paths: list[str],
        property_filter: str | None,
        max_tokens_total: int | None,
        max_tokens_per_property: int | None,
    ):
        self._directory: Path = directory
        self._rel_paths: list[Path] = get_rel_paths(
            directory, filter_paths=filter_paths
        )
        if len(self._rel_paths) > max_files:
            raise ValueError(f"Too many files: {len(self._rel_paths)}")
        for p in self._rel_paths:
            logger.debug(f"Found {p}")
        self._chroma_client: ClientAPI = chromadb.Client()
        self._collection: chromadb.Collection = (
            self._chroma_client.create_collection("codebase")
        )

        documents = [
            (directory / rel_path).read_text() for rel_path in self._rel_paths
        ]
        ids = [str(rel_path) for rel_path in self._rel_paths]
        self._collection.add(documents=documents, ids=ids)

        self._property_locations = []
        for p in self._rel_paths:
            props = extract_propositions(
                (directory / p).read_text(), filter=property_filter
            )
            for prop in props:
                self._property_locations.append(
                    PropertyLocation(rel_path=p, line_num=prop.line_num)
                )
                logger.debug(f"Found property @ {p}:{prop.line_num}")
                logger.debug(f"Prop: {prop.statement}")

        self._max_messages = max_messages
        self._exclude_full_files = (
            sum(len(doc) for doc in documents)
            >= min_length_to_exclude_full_files
        )

        python_deps = get_deps_rec(
            self._directory, self._directory, self._rel_paths
        )
        if python_deps:
            self._python_deps_text = f"""Here is the dependency graph of the codebase:
{json.dumps(python_deps, indent=2)}

"""
        else:
            self._python_deps_text = ""

        self._max_tokens_total = max_tokens_total
        self._max_tokens_per_property = max_tokens_per_property

    def _build_initial_message(
        self, property_location: PropertyLocation
    ) -> str:
        correctness_blurb = """By "correct", we mean very high confidence that each step of the proof is valid,
the proof does in fact prove the proposition, and that the proof is supported by
what the code does. Mark the proof as "incorrect" if you understand it and the
code but the proof is wrong. Use "unknown" if e.g. you don't 100% know how an
external library works, or the proof needs more detail. Skeptically and
rigorously check every claim with references to the code. If the proof
references an explicitly-stated axiom (or "assumption", etc), you can assume
that the axiom is correct."""

        if not self._exclude_full_files:
            return f"""The following is a listing of all files in a codebase:
{"\n".join(str(p) for p in self._rel_paths)}

At the end of this message is a listing of all file contents.

The file {property_location.rel_path} contains one or more propositions
about the codebase. The proposition we are interested in is on line
{property_location.line_num}, and is followed by a proof.

In your response, state whether the proof (not the proposition) is correct.
{correctness_blurb}
        

File contents:
{"\n".join(format_file((self._directory / p).read_text(), rel_path=p) for p in self._rel_paths)}
            """
        else:
            return f"""The following is a listing of all files in a codebase:
{"\n".join(str(p) for p in self._rel_paths)}

{self._python_deps_text}

At the end of this message is a listing of the contents of {property_location.rel_path}.
This file contains one or more propositions
about the codebase. The proposition we are interested in is on line
{property_location.line_num}, and is followed by a proof.

The goal of this conversation is to determine whether the proof 
(not the proposition) is correct.

Your responses in this conversation can be one of the following.

1. Request files

In this response, you may request to see additional files from the codebase in
order to ultimately determine whether the proof is correct. They will be
provided to you in the next message. You will have the opportunity to request
further files if needed, and we will repeat this process until you are ready to
make a final determination. You can request up to {MAX_FILES_REQUESTED} files
at a time.

2. Correctness verdict

In this response, state whether the proof is correct.
{correctness_blurb}
(Use this response only if you have seen enough of
the codebase to make a determination.)

File contents:
{format_file((self._directory / property_location.rel_path).read_text(), rel_path=property_location.rel_path)}
            """

    def _build_subsequent_message(self, files_to_show: Sequence[Path]) -> str:
        return f"""The requested files are given below.

{"\n".join(format_file((self._directory / p).read_text(), rel_path=p) for p in files_to_show)}
        """

    def check_proofs(self) -> list[ProofCheckResult]:
        ret = []
        tokens_used = 0
        for property_location in self._property_locations:
            if (
                self._max_tokens_per_property is None
                and self._max_tokens_total is None
            ):
                max_tokens = None
            elif self._max_tokens_per_property is None:
                max_tokens = min(
                    self._max_tokens_total - tokens_used, MAX_CONTEXT_LENGTH
                )
            elif self._max_tokens_total is None:
                max_tokens = min(
                    self._max_tokens_per_property, MAX_CONTEXT_LENGTH
                )
            else:
                max_tokens = min(
                    self._max_tokens_per_property,
                    self._max_tokens_total - tokens_used,
                    MAX_CONTEXT_LENGTH,
                )
            pcr, tokens_just_used = self.check_proof(
                property_location,
                max_tokens,
            )
            ret.append(pcr)
            tokens_used += tokens_just_used

        return ret

    def check_proof(
        self, property_location: PropertyLocation, max_tokens: int | None
    ) -> tuple[ProofCheckResult, int]:
        files_shown = set()
        all_files = set(self._rel_paths)

        input_tokens_in_messages = 0
        input_tokens_sent = 0
        completion_tokens_used = 0

        initial_message = self._build_initial_message(property_location)
        logger.debug(initial_message)
        messages = [
            {
                "role": "user",
                "content": initial_message,
            }
        ]

        for _ in range(self._max_messages):
            input_tokens_in_messages = sum(
                len(enc.encode(m["content"])) + MESSAGE_ENVELOPE_TOKENS
                for m in messages
                if m["role"] == "user"
            )
            if max_tokens is None:
                max_completion_tokens_key = dict()
            else:
                max_completion_tokens = (
                    max_tokens
                    - completion_tokens_used
                    - input_tokens_in_messages
                )
                if max_completion_tokens <= 0:
                    return (
                        ProofCheckResult(
                            property_location=property_location,
                            correctness_explanation=Failure(
                                msg="Token limit reached"
                            ),
                        ),
                        completion_tokens_used + input_tokens_sent,
                    )
                max_completion_tokens_key = {
                    "max_completion_tokens": max_completion_tokens,
                }
            try:
                resp = openai_client.beta.chat.completions.parse(
                    **MODEL,
                    **max_completion_tokens_key,  # type: ignore
                    messages=messages,  # type: ignore
                    response_format=(
                        FullFilesExcludedResponse
                        if self._exclude_full_files
                        else FullFilesIncludedResponse
                    ),
                )
            except (BadRequestError, LengthFinishReasonError) as e:
                return (
                    ProofCheckResult(
                        property_location=property_location,
                        correctness_explanation=Failure(msg=str(e)),
                    ),
                    # a guess
                    max_tokens or MAX_CONTEXT_LENGTH,
                )
            logger.debug(f"Token usage: {resp.usage}")
            if resp.usage is None:
                raise RuntimeError("No token usage available")
            completion_tokens_used += resp.usage.completion_tokens
            input_tokens_sent = input_tokens_in_messages

            resp_message = resp.choices[0].message
            if resp_message.content is None:
                raise RuntimeError("No response from LLM")
            logger.debug(resp_message.content)
            messages.append(
                {"role": "assistant", "content": resp_message.content}
            )
            if resp_message.parsed is None:
                raise RuntimeError("No response from LLM")
            response_data = resp_message.parsed.data

            if isinstance(response_data, CorrectnessExplanation):
                return (
                    ProofCheckResult(
                        property_location=property_location,
                        correctness_explanation=response_data,
                    ),
                    completion_tokens_used + input_tokens_sent,
                )
            else:
                files_requested = sorted(
                    (
                        all_files
                        & {Path(p) for p in response_data.files_requested}
                    )
                    - files_shown
                )[:MAX_FILES_REQUESTED]
                files_shown.update(files_requested)

                if not files_requested:
                    raise RuntimeError("Invalid response")

                subsequent_message = self._build_subsequent_message(
                    files_requested
                )
                logger.debug(subsequent_message)
                messages.append(
                    {
                        "role": "user",
                        "content": subsequent_message,
                    }
                )
        else:
            raise TimeoutError("No result after max messages")


def main(
    directory: Annotated[
        Path,
        typer.Argument(
            help="Repo to analyze",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = Path("."),
    max_files: Annotated[
        int, typer.Option(help="Max number of files in the codebase")
    ] = 1_000,
    max_messages: Annotated[
        int,
        typer.Option(
            help="Max number of messages in each LLM conversation before aborting"
        ),
    ] = 50,
    min_length_to_exclude_full_files: Annotated[
        int,
        typer.Option(
            help="Min size of codebase (in characters) such that we do not include all file contents in the initial prompt"
        ),
    ] = 100_000,
    filter_path: Annotated[
        list[str] | None,
        typer.Option(
            help="Path to exclude from the analysis in .gitignore format. Repeat as needed.",
        ),
    ] = None,
    property_filter: Annotated[
        str | None,
        typer.Option(
            help="Natural language instructions on which properties to check."
        ),
    ] = None,
    max_tokens_total: Annotated[
        int | None,
        typer.Option(help="Max number of tokens to use (approximate!)"),
    ] = None,
    max_tokens_per_property: Annotated[
        int | None,
        typer.Option(
            help="Max number of tokens to use per property (approximate!)"
        ),
    ] = None,
):
    linter = Linter(
        directory=directory,
        max_files=max_files,
        max_messages=max_messages,
        min_length_to_exclude_full_files=min_length_to_exclude_full_files,
        filter_paths=filter_path or [],
        property_filter=property_filter,
        max_tokens_total=max_tokens_total,
        max_tokens_per_property=max_tokens_per_property,
    )
    results = linter.check_proofs()
    print(
        json.dumps(
            [result.model_dump(mode="json") for result in results],
            indent=2,
        )
    )
    if any(
        isinstance(result.correctness_explanation, Failure)
        or result.correctness_explanation.correctness != "correct"
        for result in results
    ):
        exit(1)


def cli() -> int:
    typer.run(main)
    return 0


if __name__ == "__main__":
    exit(cli())
