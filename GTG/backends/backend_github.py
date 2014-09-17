# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# Getting Things GNOME! - a personal organizer for the GNOME desktop
# Copyright (c) 2008-2013 - Lionel Dricot & Bertrand Rousseau
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <http://www.gnu.org/licenses/>.
# -----------------------------------------------------------------------------

'''
Backend for importing GitHub issues in GTG
'''

import os
import uuid
from github3 import repository, iter_repo_issues, GitHubError
from dateutil.tz import tzutc, tzlocal

from GTG.core.task import Task
from GTG import _
from GTG.backends.genericbackend import GenericBackend
from GTG.backends.syncengine import SyncEngine, SyncMeme
from GTG.tools.logger import Log
from GTG.backends.periodicimportbackend import PeriodicImportBackend
from GTG.tools.dates import Date


class Backend(PeriodicImportBackend):
    '''GitHub backend, capable of importing GitHub issues in GTG.'''

    _general_description = {
        GenericBackend.BACKEND_NAME: "backend_github",
        GenericBackend.BACKEND_HUMAN_NAME: _("GitHub"),
        GenericBackend.BACKEND_AUTHORS: ["Thomas Perret"],
        GenericBackend.BACKEND_TYPE: GenericBackend.TYPE_READONLY,
        GenericBackend.BACKEND_DESCRIPTION:
        _("This synchronization service lets you import the issues"
          " of a repository hosted on GitHub in GTG. As the"
          " issue state changes in GitHub, the GTG task is "
          " updated.\n"
          "Please note that this is a read only synchronization service,"
          " which means that if you open one of the imported tasks and "
          " change one of the:\n"
          "  - title\n"
          "  - description\n"
          "  - tags\n"
          "Your changes <b>will</b> be reverted when the associated"
          " bug is modified."),
    }

    _static_parameters = {
        "username": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_STRING,
            GenericBackend.PARAM_DEFAULT_VALUE: "insert the username here"},
        "repository": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_STRING,
            GenericBackend.PARAM_DEFAULT_VALUE: "insert the repository here"},
        "period": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_INT,
            GenericBackend.PARAM_DEFAULT_VALUE: 10, },
        "import-bug-tags": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_BOOL,
            GenericBackend.PARAM_DEFAULT_VALUE: True},
        "group-tasks": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_BOOL,
            GenericBackend.PARAM_DEFAULT_VALUE: True},
    }

###############################################################################
### Backend standard methods ##################################################
###############################################################################
    def __init__(self, parameters):
        '''
        See GenericBackend for an explanation of this function.
        Re-loads the saved state of the synchronization
        '''
        super(Backend, self).__init__(parameters)
        # loading the saved state of the synchronization, if any
        self.data_path = os.path.join('backends/github/',
                                      "sync_engine-" + self.get_id())
        self.sync_engine = self._load_pickled_file(self.data_path,
                                                   SyncEngine())
        self.master_tid = None

    def initialize(self):
        """
        See GenericBackend for an explanation of this function.
        """
        super(Backend, self).initialize()
        if self._parameters["group-tasks"]:
            try:
                repo = repository(self._parameters["username"],
                                  self._parameters["repository"])
                master_url = repo.html_url
            except GitHubError:
                master_url = "https://github.com/%s/%s" % \
                        (self._parameters["username"],
                         self._parameters["repository"])

            master_tid = str(uuid.uuid5(namespace=uuid.NAMESPACE_URL,
                             name=master_url))
            if not self.datastore.has_task(master_tid):
                master_task = self.datastore.task_factory(master_tid, True)
                master_task.title = _("GitHub repository: ") + '%s/%s' % \
                                        (self._parameters["username"],
                                         self._parameters["repository"])
                master_task.set_text(master_url)
                self.datastore.push_task(master_task)
            self.master_tid = master_tid

    def do_periodic_import(self):
        '''
        See GenericBackend for an explanation of this function.
        Connect to GitHub and updates the state of GTG tasks to reflect the
        issues on GitHub.
        '''

        # Fetching issues
        self.cancellation_point()
        try:
            issues_tasks = iter_repo_issues(self._parameters["username"],
                                            self._parameters["repository"],
                                            state="all")
        except GitHubError as msgerr:
            Log.debug(msgerr)
            return

        # Adding and updating
        for issue in issues_tasks:
            self.cancellation_point()
            self._process_github_issue(issue)

        # removing the old ones
        last_issue_list = self.sync_engine.get_all_remote()
        new_issue_list = [issue.html_url for issue in issues_tasks]
        for issue_link in set(last_issue_list).difference(set(new_issue_list)):
            self.cancellation_point()
            # we make sure that the other backends are not modifying the task
            # set
            with self.datastore.get_backend_mutex():
                tid = self.sync_engine.get_local_id(issue_link)
                if tid != self.master_tid:
                    self.datastore.request_task_deletion(tid)
                try:
                    self.sync_engine.break_relationship(remote_id=issue_link)
                except KeyError:
                    pass

    def save_state(self):
        '''Saves the state of the synchronization'''
        self._store_pickled_file(self.data_path, self.sync_engine)

###############################################################################
### Process tasks #############################################################
###############################################################################
    def _process_github_issue(self, issue):
        '''
        Given an issue object, finds out if it must be synced to a GTG note and,
        if so, it carries out the synchronization (by creating or
        updating a GTG task, or deleting itself if the related task has
        been deleted)

        @param note: a github issue
        '''
        has_task = self.datastore.has_task
        action, tid = self.sync_engine.analyze_remote_id(issue.html_url,
                                                         has_task,
                                                         lambda b: True)
        Log.debug("processing github (%s)" % (action))

        if action is None:
            return

        issue_dic = self._prefetch_issue_data(issue)
        # for the rest of the function, no access to issue must be made, so
        # that the time of blocking inside the with statements is short.
        # To be sure of that, set issue to None
        issue = None

        with self.datastore.get_backend_mutex():
            if action == SyncEngine.ADD:
                tid = str(uuid.uuid4())
                task = self.datastore.task_factory(tid)
                self._populate_task(task, issue_dic)
                self.sync_engine.record_relationship(local_id=tid,
                                                     remote_id=str(
                                                     issue_dic['self_link']),
                                                     meme=SyncMeme(
                                                     task.get_modified(),
                                                     issue_dic['modified'],
                                                     self.get_id()))
                self.datastore.push_task(task)

            elif action == SyncEngine.UPDATE:
                task = self.datastore.get_task(tid)
                self._populate_task(task, issue_dic)
                meme = self.sync_engine.get_meme_from_remote_id(
                    issue_dic['self_link'])
                meme.set_local_last_modified(task.get_modified())
                meme.set_remote_last_modified(issue_dic['modified'])
        self.save_state()

    def _populate_task(self, task, issue_dic):
        '''
        Fills a GTG task with the data from a GitHub issue.

        @param task: a Task
        @param bug: a GitHub issue dictionary, generated with
                    _prefetch_issue_data
        '''
        # set task status
        if self._parameters["group-tasks"]:
            task.set_parent(self.master_tid)
        if issue_dic["completed"]:
            task.set_status(Task.STA_DONE,
                            donedate=Date(issue_dic["closed"].date()))
        else:
            task.set_status(Task.STA_ACTIVE)
        if task.get_title() != issue_dic['title']:
            task.set_title("GH #%s: " % issue_dic["number"]
                           + issue_dic['title'])
        text = self._build_bug_text(issue_dic)
        if task.get_excerpt() != text:
            task.set_text(text)
        new_tags_sources = []
        if self._parameters["import-bug-tags"]:
            new_tags_sources += issue_dic['tags']
        new_tags = set(['@' + str(tag) for tag in new_tags_sources])
        current_tags = set(task.get_tags_name())
        # remove the lost tags
        for tag in current_tags.difference(new_tags):
            task.remove_tag(tag)
        # add the new ones
        for tag in new_tags.difference(current_tags):
            task.add_tag(tag)
        task.add_remote_id(self.get_id(), issue_dic['self_link'])

    def _prefetch_issue_data(self, issue):
        '''
        We fetch all the necessary info that we need from the bug to populate a
        task beforehand (these will be used in _populate_task).
        This function takes a long time to complete (all access to bug data are
        requests on then net), but it can crash without having the state of the
        related task half-changed.

        @param bug: a github issue task
        @returns dict: a dictionary containing the relevant issue attributes
        '''
        owner = issue.user
        issue_dic = {'title': issue.title,
                   'text': issue.body_text,
                   'tags': issue.labels,
                   'self_link': issue.html_url,
                   'opened': self._tz_utc_to_local(issue.created_at),
                   'closed': self._tz_utc_to_local(issue.closed_at),
                   'modified': self._tz_utc_to_local(issue.updated_at),
                   'owner_login': owner.login,
                   'owner_name': owner.name,
                   'completed': issue.is_closed(),
                   'completed_by': issue.closed_by,
                   'status': issue.state,
                   'number': issue.number,
                   'milestone': issue.milestone}
        return issue_dic

    def _tz_utc_to_local(self, date):
        """ Convert GitHub UTC date to local format """
        if date is not None:
            date = date.replace(tzinfo=tzutc())
            date = date.astimezone(tzlocal())
            return date.replace(tzinfo=None)
        else:
            return None

#    def _tz_local_to_utc(self, dt):
#        dt = dt.replace(tzinfo=tzlocal())
#        dt = dt.astimezone(tzutc())
#        return dt.replace(tzinfo=None)

    def _build_bug_text(self, issue_dic):
        '''
        Creates the text that describes an issue
        '''
        text = _("Reported by: ") + '%s' % \
            issue_dic["owner_login"] + '\n'
        text += _("Link to issue: ") + \
            "%s" % \
            (issue_dic["self_link"]) + '\n'
        text += _("Opened: ") + '%s' % issue_dic["opened"] + '\n'
        text += _("Last modified: ") + '%s' % issue_dic["modified"] + '\n'
        text += _("Milestone :") + '%s' % issue_dic["milestone"] + '\n'
        if issue_dic["text"] is not None:
            text += '\n' + issue_dic["text"]
        return text
