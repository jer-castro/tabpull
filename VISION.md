# Vision

This document describes the target state, not current behavior, and the gaps are the roadmap.
tabpull exists so that a person can export a Tableau dashboard sheet as a crosstab CSV, including a hidden sheet, with the filters they would have set by hand.
It serves someone who is allowed to crosstab a view and may not be allowed to download the workbook.
It turns a jobs file and a Tableau session into one UTF-8 CSV per named sheet.
It owns exactly one thing: the installed tabpull command that sets up a site, adds a job, removes a job, runs it, and refreshes the sign-in.

## One command

A person installs it with uv tool install and runs tabpull.
setup, add, remove, run, and login are subcommands of that one command.
Running a source file is not a supported way to use it.
The command runs once and exits, so a scheduler outside the tool can call it.
tabpull has no interval, no wait loop, no daemon, and no scheduler of its own.
Importing its Python modules is not a supported interface.

## The crosstab a person would download

The export is the crosstab from Download > Crosstab > CSV, not the workbook and not a data extract.
Every sheet, including a published worksheet, is exported through the Embedding API.
REST summary data is not a second export, because it is not a crosstab and the embed path already returns the sheet.
A story is refused, and the supported target is the dashboard inside it.
A dashboard-only sheet (a hidden sheet, with no published view of its own), or a sheet that is not first alphabetically, is exported by name like any other sheet.

## Filters behave like the dashboard

Parameters are set first, then filters, in the order they are written.
A filter names the sheet it applies on.
A filter that names no sheet applies on the first sheet in the job, and the command says so.
It does not apply that filter to every sheet in silence.
It reaches other sheets only the way that same filter does in the dashboard.
A date written as YYYY-MM-DD or M/D/YYYY is that calendar day in every browser timezone.
An impossible date is rejected rather than shifted.
A relative date, or a date computed at run time, is refused.
That case is a parameter, or a job edited by hand.

## Finding a view and recording a job

add accepts a pasted view URL, which selects that view, or a loose name, which lists the matches and waits for a pick.
The loose name stays, because that is how a person finds a dashboard.
add also accepts the site, the view, the sheets, and the filters as flags, and then writes the job without prompts.
When those flags are absent, add still asks.
add records the job and does not export until asked.
remove deletes named jobs and rewrites the jobs file.
A terminal with no names asks which jobs to delete.
No terminal and no names stops and asks for the names.
An unknown name leaves the file unchanged.
Removing every job leaves the file empty.
run exports the selected jobs, keeps going when one fails, and exits non-zero if any did.

## More than one site

One install stores more than one Tableau server and site.
Each site has its own token and its own browser session, and each job names the site it uses.
Several sites are the same command, not a second tool.

## Files stay in a conventional place

The token, the SSO cookies, and the jobs file live in the config directory, the XDG base directory or the equivalent on each system.
They do not follow the working directory.
Exports land in the working directory, where the person running the tool expects them.
A flag may point one run at a different jobs file or a different output folder.
Those locations are documented, and deleting them removes the tool's files from the machine.
The tool may print the server and the site, and it does not print the token.
A missing or expired SSO session opens the sign-in window when a person is at a terminal.
Otherwise it stops and names the command that refreshes the session.
The browser is installed Chrome, then installed Edge.
Playwright Chromium is the fallback when neither of those is installed.
The tool does not download a browser while Chrome or Edge is already there.
If none of them launches, it stops and names the install step.

## Scope

tabpull is not a general Tableau client, not a workbook downloader, not a viz image renderer, and not a scheduler.
It does not export a story, a relative date, a date computed at run time, or REST summary data.
A change to export, filter, or search behavior is driven against the real Tableau site before it ships.
The proof is the file that was written and a control export that must differ.
Row contents are not the proof.

A change aligns when a person can install tabpull and, from any directory, export a named dashboard sheet, including a hidden one, as the UTF-8 crosstab they would download by hand, for the site that job names, with the filters and parameters they named.
A change should be resisted when it adds a second way to run the tool, a scheduler, a supported Python import, a silent broad filter, or an export that is not that sheet's crosstab.
