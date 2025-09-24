# CMS Project gitlab FAQ

## Where is my repo?

(needs updating for each cohort)

https://campus.cs.le.ac.uk/gitlab/pgt_project/25_26_Autumn/UID

## Who has access?

You only have access to your own project repository. CMS staff
involved in the project also have access.

## Login details

* Use the "Informatics student LDAP" login tab (*NOT* the "Standard"
  login tab).
* Username: `UID` (without @student.le.ac.uk)
* Password: same as normal

Gitlab has an automatic anti-brute-force-login mechanism. If you fail
to login 5 times within 10 minutes your gitlab account will be blocked
from all actions for 10 more minutes.

## Quota

The default quota is 3Gb. This quota is the total size of your repository
(ie, it includes all of your commit history, etc)

If you need a larger quota you will need to provide a justification,
and the support of your supervisor and/or the module team.

See also the sections later on ignoring irrelevant files/folders and
on how to deal with large data files.

## Push your changes

You are strongly advised to push your changes to gitlab regularly
(ideally after every session where you work on your project).

This means that you will remain familiar with the process, and will
alert you more quickly if there are any problems in the way you are
using gitlab.

### Use the tools provided by your development environment

All modern IDE tools have integrated git connectivity. You should make
use of this facility in almost all cases (for example, rather than
relying on basic command line use of git to manage your code).

You will probably find it convenient to create a gitlab access token
to facilitate your IDE's gitlab connection.

## Your repository should correspond to your actual working environment

The files in your repository should be the actual files that you work
with directly. Your gitlab repository should be able to trace the
progress of your work at the level of individual files.

You *should not* be periodically making a `zip` file of your project
status and committing and pushing it to git.

### Marking milestones

If you want to record significant points in the development of your
project (for example the status on one of the project modules
milestones) then you should create a `branch` marking that stage.

## Ignore non-essential files

The important files in your gitlab repository are the files
(configuration, code, documentation, tests, etc) that you have written
yourself and which represent the actual work you have done towards
your project.

There are generally many other files associated with your project that
do not need to be committed to git or gitlab.

You can control which files don't get included in your git history and
pushed to gitlab using `.gitignore` files.

For example:

* Files that can be automatically re-generated. There are many
  examples:
  - Compiled code files such as `.class` (for java), `.pyc`,
    `__pycache__` (python), `.o` (C, C++)
  - Your IDE may organise compiled code into a different folder to
    source code - for example source in `src` (keep in git) and
    compiled in `bin` (do not keep in git).
  - Various configuration and cache files created by your IDE (but
    take care to include the actual project settings file, `Makefile`
    and similar).
* Components from 3rd parties that you are making use of. For example:
  - Repositories downloaded from elsewhere.
  - Libraries or tools you are making use of. Many languages have
    features that make it easy to download and use code from central
    libraries. There is no need to include this code in your git
    repository - although you should document which libraries (and
    versions) you are making use of.
* File system artefacts - for example MacOS systems always have a hidden
  `.DS_Store` file in every folder.

## Large data files

You need to carefully consider how you will handle large data files
associated with your project. See also the section "Ignore
non-essential files".

If the end product of your project involves the creation of very large
data files (eg models; animations or other video data; etc) then you
should *not* push them to your gitlab repository, but arrange some
other way to share them with your supervisor and the module assessment
team. For example, uploading to your university OneDrive account and
sharing.

### git-lfs (Large File Storage)

The gitlab server has LFS (Large File Storage) enabled and you are
encouraged to use it for data files in general. See [the git-lfs home page](https://git-lfs.com/)

LFS can mitigate the growth in size of your project commit history -
but you should note that the files still contribute to your quota
usage.

### git-annex

The CMS gitlab installation does not directly support
[git-annex](https://git-annex.branchable.com/), but you may be able
use it with your local copy of the repository to automate support for
storing large data files remotely without needing to commit them
directly to gitlab.

[This
page](https://numpex-pc5.gitlabpages.inria.fr/wp2-co-design/doc-hub/large-files/git-annex/)
also provides a discussion of the issues and some examples of
git-annex in use.

(If you have direct experience with git-annex, please get in touch so
we can update this FAQ.)
