"""
Main processing for meticulous
"""
from __future__ import absolute_import, division, print_function

import io
import os
import re
import shutil
import sys
from pathlib import Path

from plumbum import FG, local
from PyInquirer import prompt
from spelling.check import check  # noqa=I001

from meticulous._github import (
    check_forked,
    checkout,
    fork,
    get_api,
    is_archived,
    issues_allowed,
)
from meticulous._sources import obtain_sources
from meticulous._storage import get_json_value, prepare, set_json_value


def make_simple_choice(choices, message="What do you want to do?"):
    """
    Make a choice using a simple {key: key} list of choices
    """
    return make_choice({choice: choice for choice in choices}, message=message)


def make_choice(choices, message="What do you want to do?"):
    """
    Call PyInquirer/prompt-toolkit to make a choice
    """
    choicelist = sorted(list(choices.keys()))
    choicelist.append("- quit -")
    menu = [
        {"type": "list", "name": "option", "message": message, "choices": choicelist}
    ]
    answers = prompt(menu)
    option = answers.get("option", "- quit -")
    return choices.get(option)


class ProcessingFailed(Exception):
    """
    Raised if processing needs to go back to the main menu
    """


class NoRepoException(ProcessingFailed):
    """
    Raised if no repositories are available/selected
    """


def run_invocation(target):
    """
    Execute the invocation
    """
    if target is None:
        target = Path(os.environ["HOME"]) / "data"
    else:
        target = Path(target)
    if not target.is_dir():
        print(f"Target {target} is not a directory.", file=sys.stderr)
        sys.exit(1)
    prepare()
    while True:
        try:
            lookup = {
                "examine a repository": examine_repo_selection,
                "manually add a new repository": manually_add_new_repo,
                "remove a repository": remove_repo_selection,
                "add a new repository": add_new_repo,
                "prepare a change": prepare_a_change,
                "prepare a pr/issue": prepare_a_pr_or_issue,
            }
            handler = make_choice(lookup)
            if handler is None:
                print("Goodbye.")
                return
            handler(target)
        except ProcessingFailed:
            continue


def remove_repo_selection(target):  # pylint: disable=unused-argument
    """
    Select an available repository to remove
    """
    os.chdir(target)
    repo, repodir = pick_repo()
    for name in ("repository_map", "repository_saves"):
        repository_map = get_json_value(name, {})
        try:
            del repository_map[repo]
            set_json_value(name, repository_map)
        except KeyError:
            continue
    option = make_simple_choice(["Yes", "No"], "Delete the directory?")
    if option == "Yes":
        shutil.rmtree(repodir)


def examine_repo_selection(target):  # pylint: disable=unused-argument
    """
    Select an available repository to examine
    """
    _, repodir = pick_repo()
    examine_repo(repodir)


def prepare_a_change(target):  # pylint: disable=unused-argument
    """
    Select an available repository to prepare a change
    """
    _, repodir = pick_repo()
    add_change_for_repo(repodir)


def prepare_a_pr_or_issue(target):  # pylint: disable=unused-argument
    """
    Select an available repository to prepare a change
    """
    reponame, reposave = pick_repo_save()
    while True:
        repodir = reposave["repodir"]
        repodirpath = Path(repodir)
        choices = get_pr_or_issue_choices(reponame, repodirpath)
        option = make_choice(choices)
        if option is None:
            return
        handler, context = option
        handler(reponame, reposave, context)


def get_pr_or_issue_choices(reponame, repodirpath):
    """
    Work out the choices menu for pr/issue
    """
    issue_template = Path(".github") / "ISSUE_TEMPLATE"
    pr_template = Path(".github") / "pull_request_template.md"
    contrib_guide = Path("CONTRIBUTING.md")
    issue = Path("__issue__.txt")
    commit = Path("__commit__.txt")
    prpath = Path("__pr__.txt")
    no_issues = Path("__no_issues__.txt")
    choices = {}
    for path in (
        issue_template,
        pr_template,
        contrib_guide,
        prpath,
        issue,
        commit,
        no_issues,
    ):
        has_path = (repodirpath / path).exists()
        print(f"{reponame} {'HAS' if has_path else 'does not have'}" f" {path}")
        if has_path:
            choices[f"show {path}"] = (show_path, path)
    repo_disables_issues = (repodirpath / no_issues).exists()
    if repo_disables_issues:
        choices["make a commit"] = (make_a_commit, False)
    else:
        choices["make a full issue"] = (make_issue, True)
        choices["make a short issue"] = (make_issue, False)
    has_issue = (repodirpath / issue).exists()
    if has_issue:
        choices["submit issue"] = (submit_issue, None)
    has_commit = (repodirpath / commit).exists()
    if has_commit:
        choices["submit commit"] = (submit_commit, None)
        choices["submit issue"] = (submit_issue, None)
    return choices


def make_issue(reponame, reposave, is_full):  # pylint: disable=unused-argument
    """
    Prepare an issue template file
    """
    add_word = reposave["add_word"]
    del_word = reposave["del_word"]
    file_paths = reposave["file_paths"]
    repodir = Path(reposave["repodir"])
    files = ", ".join(file_paths)
    title = f"Fix simple typo: {del_word} -> {add_word}"
    if is_full:
        body = f"""\
# Issue Type

[x] Bug (Typo)

# Steps to Replicate

1. Examine {files}.
2. Search for `{del_word}`.

# Expected Behaviour

1. Should read `{add_word}`.
"""
    else:
        body = f"""\
There is a small typo in {files}.
Should read `{add_word}` rather than `{del_word}`.
"""
    with io.open(str(repodir / "__issue__.txt"), "w", encoding="utf-8") as fobj:
        print(title, file=fobj)
        print("", file=fobj)
        print(body, file=fobj)


def make_a_commit(reponame, reposave, is_full):  # pylint: disable=unused-argument
    """
    Prepare a commit template file
    """
    add_word = reposave["add_word"]
    del_word = reposave["del_word"]
    file_paths = reposave["file_paths"]
    repodir = Path(reposave["repodir"])
    files = ", ".join(file_paths)
    commit_path = str(repodir / "__commit__.txt")
    with io.open(commit_path, "w", encoding="utf-8") as fobj:
        print(
            f"""\
Fix simple typo: {del_word} -> {add_word}

There is a small typo in {files}.
Should read `{add_word}` rather than `{del_word}`.
""",
            file=fobj,
        )


def submit_issue(reponame, reposave, ctxt):  # pylint: disable=unused-argument
    """
    Push up an issue
    """
    repodir = Path(reposave["repodir"])
    add_word = reposave["add_word"]
    del_word = reposave["del_word"]
    issue_path = str(repodir / "__issue__.txt")
    title, body = load_commit_like_file(issue_path)
    issue_num = issue_via_api(reponame, title, body)
    commit_path = str(repodir / "__commit__.txt")
    with io.open(commit_path, "w", encoding="utf-8") as fobj:
        print(
            f"""\
Fix simple typo: {del_word} -> {add_word}

Closes #{issue_num}
""",
            file=fobj,
        )


def issue_via_api(reponame, title, body):
    """
    Create an issue via the API
    """
    api = get_api()
    user_org = api.get_user().login
    repo = api.get_repo(f"{user_org}/{reponame}")
    while repo.parent:
        repo = repo.parent
    issue = repo.create_issue(title=title, body=body)
    return issue.number


def load_commit_like_file(path):
    """
    Read title and body from a well formatted git commit
    """
    with io.open(path, "r", encoding="utf-8") as fobj:
        title = fobj.readline().strip()
        blankline = fobj.readline().strip()
        if blankline != "":
            raise Exception(f"Needs to be a blank second line for {path}.")
        body = fobj.read()
    return title, body


def submit_commit(reponame, reposave, ctxt):  # pylint: disable=unused-argument
    """
    Push up a commit
    """
    repodir = Path(reposave["repodir"])
    add_word = reposave["add_word"]
    commit_path = str(repodir / "__commit__.txt")
    title, body = load_commit_like_file(commit_path)
    from_branch, to_branch = push_commit(repodir, add_word)
    create_pr(reponame, title, body, from_branch, to_branch)


def push_commit(repodir, add_word):
    """
    Create commit and push
    """
    git = local["git"]
    with local.cwd(repodir):
        to_branch = git("symbolic-ref", "--short", "HEAD").strip()
        from_branch = f"bugfix/typo_{add_word}"
        _ = git["commit", "-F", "__commit__.txt"] & FG
        _ = git["push", "origin", f"{to_branch}:{from_branch}"] & FG
    return from_branch, to_branch


def create_pr(reponame, title, body, from_branch, to_branch):
    """
    Use API to create a pull request
    """
    api = get_api()
    user_org = api.get_user().login
    repo = api.get_repo(f"{user_org}/{reponame}")
    while repo.parent:
        repo = repo.parent
    pullreq = repo.create_pull(
        title=title, body=body, base=to_branch, head=f"{user_org}:{from_branch}"
    )
    print(f"Created PR #{pullreq.number} view at" f" {pullreq.html_url}")


def show_path(reponame, reposave, path):  # pylint: disable=unused-argument
    """
    Display the issue template directory
    """
    print("Opening editor")
    editor = local["vim"]
    repodir = reposave["repodir"]
    with local.cwd(repodir):
        _ = editor[str(path)] & FG


def add_change_for_repo(repodir):
    """
    Work out the staged commit and prepare an issue and pull request based on
    the change
    """
    del_word, add_word, file_paths = get_typo(repodir)
    print(f"Changing {del_word} to {add_word} in {', '.join(file_paths)}")
    option = make_simple_choice(["save"], "Do you want to save?")
    if option == "save":
        saves = get_json_value("repository_saves", {})
        reponame = Path(repodir).name
        saves[reponame] = {
            "add_word": add_word,
            "del_word": del_word,
            "file_paths": file_paths,
            "repodir": repodir,
        }
        set_json_value("repository_saves", saves)


def get_typo(repodir):
    """
    Look in the staged commit for the typo.
    """
    git = local["git"]
    del_lines = []
    add_lines = []
    file_paths = []
    with local.cwd(repodir):
        output = git("diff", "--staged")
        for line in output.splitlines():
            if line.startswith("--- a/"):
                index = len("--- a/")
                file_path = line[index:]
                file_paths.append(file_path)
        for line in output.splitlines():
            if line.startswith("-") and not line.startswith("--- "):
                del_lines.append(line[1:])
            elif line.startswith("+") and not line.startswith("+++ "):
                add_lines.append(line[1:])
    if not del_lines or not add_lines:
        print("Could not read diff", file=sys.stderr)
        raise ProcessingFailed()
    del_words = re.findall("\\S+", del_lines[0])
    add_words = re.findall("\\S+", add_lines[0])
    for del_word, add_word in zip(del_words, add_words):
        if del_word != add_word:
            return del_word, add_word, file_paths
    print("Could not locate typo", file=sys.stderr)
    raise ProcessingFailed()


def pick_repo_save():
    """
    Select a saved repository
    """
    return pick_repo_common("repository_saves")


def pick_repo():
    """
    Select an available repository
    """
    return pick_repo_common("repository_map")


def pick_repo_common(key):
    """
    Select an available repository
    """
    repository_list = get_json_value(key, {})
    if not repository_list:
        print("No repositories available.", file=sys.stderr)
        raise NoRepoException()
    option = make_simple_choice(repository_list, "Which Repository?")
    if option is None:
        raise NoRepoException()
    repo_data = repository_list[option]
    return option, repo_data


def examine_repo(repodir):
    """
    Inspect an available repository
    """
    print("Opening editor")
    editor = local["vim"]
    with local.cwd(repodir):
        _ = editor["spelling.txt"] & FG


def manually_add_new_repo(target):
    """
    Allow entry of a new repository manually
    """
    choices = sorted(os.listdir(target))
    option = make_simple_choice(choices, "Which Directory?")
    if option is None:
        raise NoRepoException()
    repository_map = get_json_value("repository_map", {})
    repository_map[option] = str(Path(target) / option)
    set_json_value("repository_map", repository_map)


def add_new_repo(target):
    """
    Locate a new repository and add it to the available set.
    """
    for orgrepo in obtain_sources():
        _, repo = orgrepo.split("/", 1)
        print(f"Checking {orgrepo}")
        if not check_forked(repo):
            print(f"Have not forked {orgrepo}")
            print(f"Forking {orgrepo}")
            fork(orgrepo)
            if is_archived(orgrepo):
                print(f"Skipping archived repo {orgrepo}")
                continue
            print(f"Checkout {repo}")
            checkout(repo, target)
            repodir = target / repo
            print(f"Running spell check on {repodir}")
            spellpath = repodir / "spelling.txt"
            print(f"Spelling output {spellpath}")
            with io.open(spellpath, "w", encoding="utf-8") as fobj:
                os.chdir(repodir)
                check(True, True, None, fobj)
            repository_map = get_json_value("repository_map", {})
            repository_map[repo] = str(repodir)
            set_json_value("repository_map", repository_map)
            if not issues_allowed(repo):
                no_issues_path = repodir / "__no_issues__.txt"
                with io.open(no_issues_path, "w", encoding="utf-8") as fobj:
                    print("No Issues.", file=fobj)
            return repo
    return None
