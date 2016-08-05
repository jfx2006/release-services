# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import absolute_import


import time
import sqlalchemy as sa

from relengapi_common import db


class Builds(db.Model):
    """
    A clobberable builds.
    """

    __tablename__ = 'clobberer_builds'

    id = sa.Column(sa.Integer, primary_key=True)
    branch = sa.Column(sa.String(50), index=True)
    builddir = sa.Column(sa.String(100), index=True)
    buildername = sa.Column(sa.String(100))
    last_build_time = sa.Column(
        sa.Integer,
        nullable=False,
        default=int(time.time())  # TODO: should be a callable
    )

    @classmethod
    def unique_hash(cls, branch, builddir, buildername, *args, **kwargs):
        return "{}:{}:{}".format(branch, builddir, buildername)

    @classmethod
    def unique_filter(cls, query, branch, builddir, buildername,
                      *args, **kwargs):
        return query.filter(
            cls.branch == branch,
            cls.builddir == builddir,
            cls.buildername == buildername
        )


class Times(db.Model):

    __tablename__ = 'clobberer_times'
    __table_args__ = (
        # Index to speed up lastclobber lookups
        sa.Index('ix_get_clobberer_times', 'slave', 'builddir', 'branch'),
    )

    id = sa.Column(sa.Integer, primary_key=True)
    branch = sa.Column(sa.String(50), index=True)
    builddir = sa.Column(sa.String(100), index=True)
    slave = sa.Column(sa.String(30), index=True)
    lastclobber = sa.Column(
        sa.Integer,
        nullable=False,
        default=int(time.time()),
        index=True
    )
    who = sa.Column(sa.String(50))

    @classmethod
    def unique_hash(cls, branch, slave, builddir, *args, **kwargs):
        return "{}:{}:{}".format(branch, slave, builddir)

    @classmethod
    def unique_filter(cls, query, branch, slave, builddir, *args, **kwargs):
        return query.filter(
            cls.branch == branch,
            cls.slave == slave,
            cls.builddir == builddir,
        )


def get_buildbot(session):
    """List of all buildbot branches.
    """

    branches = session.query(ClobbererBuilds.branch).distinct()

    # Users shouldn't see any branch associated with a release builddir
    branches = branches.filter(not_(
        ClobbererBuilds.builddir.startswith(BUILDBOT_BUILDDIR_REL_PREFIX)))

    branches = branches.order_by(ClobbererBuilds.branch)

    return [dict(name=branch[0],
                 builders=buildbot_branch_summary(session, branch[0]))
            for branch in branches]


def buildbot_branch_summary(session, branch):
    """Return a dictionary of most recent ClobbererTimess grouped by
       buildername.
    """
    # Isolates the maximum lastclobber for each builddir on a branch
    max_ct_sub_query = session.query(
        func.max(ClobbererTimes.lastclobber).label('lastclobber'),
        ClobbererTimes.builddir,
        ClobbererTimes.branch
    ).group_by(
        ClobbererTimes.builddir,
        ClobbererTimes.branch
    ).filter(ClobbererTimes.branch == branch).subquery()

    # Finds the "greatest n per group" by joining with the
    # max_ct_sub_query
    # This is necessary to get the correct "who" values
    sub_query = session.query(ClobbererTimes).join(max_ct_sub_query, and_(
        ClobbererTimes.builddir == max_ct_sub_query.c.builddir,
        ClobbererTimes.lastclobber == max_ct_sub_query.c.lastclobber,
        ClobbererTimes.branch == max_ct_sub_query.c.branch)).subquery()

    # Attaches builddirs, along with their max lastclobber to a
    # buildername
    full_query = session.query(
        ClobbererBuilds.buildername,
        ClobbererBuilds.builddir,
        sub_query.c.lastclobber,
        sub_query.c.who
    ).outerjoin(
        sub_query,
        ClobbererBuilds.builddir == sub_query.c.builddir,
    ).filter(
        ClobbererBuilds.branch == branch,
        not_(ClobbererBuilds.buildername.startswith(BUILDBOT_BUILDER_REL_PREFIX))  # noqa
    ).distinct().order_by(ClobbererBuilds.buildername)

    summary = dict()
    for result in full_query:
        buildername, builddir, lastclobber, who = result
        summary.setdefault(buildername, [])
        summary[buildername].append(
            ClobbererTimes(
                branch=branch,
                builddir=builddir,
                lastclobber=lastclobber,
                who=who
            )
        )
    return summary


def taskcluster_branches():
    """Dict of workerTypes per branch with their respected workerTypes
    """
    index = taskcluster.Index()
    queue = taskcluster.Queue()

    result = index.listNamespaces('gecko.v2', dict(limit=1000))

    branches = {
        i['name']: dict(name=i['name'], workerTypes=dict())
        for i in result.get('namespaces', [])
    }

    for branchName, branch in branches.items():

        # decision task might not exist
        try:
            decision_task = index.findTask(
                TASKCLUSTER_DECISION_NAMESPACE % branchName)
            decision_graph = queue.getLatestArtifact(
                decision_task['taskId'], 'public/graph.json')
        except taskcluster.exceptions.TaskclusterRestFailure:
            continue

        for task in decision_graph.get('tasks', []):
            task = task['task']
            task_cache = task.get('payload', dict()).get('cache', dict())

            provisionerId = task.get('provisionerId')
            if provisionerId:
                branch['provisionerId'] = provisionerId

            workerType = task.get('workerType')
            if workerType:
                branch['workerTypes'].setdefault(
                    workerType, dict(name=workerType, caches=[]))

                if len(task_cache) > 0:
                    branch['workerTypes'][workerType]['caches'] = list(set(
                        branch['workerTypes'][workerType]['caches'] +
                        task_cache.keys()
                    ))

    return branches
