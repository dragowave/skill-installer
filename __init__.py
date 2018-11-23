#  Copyright 2017 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from contextlib import contextmanager
from random import shuffle
import time
import json

from msm import SkillNotFound, SkillRequirementsException, \
    PipRequirementsException, SystemRequirementsException, CloneException, \
    GitException, AlreadyRemoved, AlreadyInstalled, MsmException, SkillEntry, \
    MultipleSkillMatches, MycroftSkillsManager
from mycroft import intent_file_handler, MycroftSkill
from mycroft.skills.skill_manager import SkillManager
from mycroft.api import DeviceApi, is_paired

class SkillInstallerSkill(MycroftSkill):
    def __init__(self):
        super().__init__()
        self.msm = SkillManager.create_msm()
        self.install_word = self.remove_word = None

    def initialize(self):
        self.settings.set_changed_callback(self.on_web_settings_change)
        try:
            if is_paired():
                self.on_web_settings_change()
        except Exception as e:
            self.log.warning('Couldn\'t run market place installer'
                             '({})'.format(repr(e)))
        self.install_word, self.remove_word = self.translate_list('action')

    @intent_file_handler('install.intent')
    def install(self, message):
        # Failsafe if padatious matches without skill entity.

        if not message.data.get('skill'):
            return self.handle_list_skills(message)

        with self.handle_msm_errors(message.data['skill'], self.remove_word):
            skill = self.find_skill(message.data['skill'], False)
            skills_data = SkillManager.load_skills_data()
            skill_data = skills_data.setdefault(skill.name, {})
            was_beta = skill_data.get('beta')

            if not was_beta and skill.is_local:
                raise AlreadyInstalled(skill.name)

            if skill.is_local:
                dialog = 'install.reinstall.confirm'
            else:
                dialog = 'install.confirm'

            if not self.confirm_skill_action(skill, dialog):
                return

            if skill.is_local:
                skill.remove()
                skill.install()
            else:
                skill.install()

            skill_data['beta'] = False
            skill_data['name'] = skill.name
            skill_data['origin'] = 'voice'
            skill_data['installation'] = 'installed'
            skill_data['installed'] = time.time()
            skill_data['failure-message'] = ''
            SkillManager.write_skills_data(skills_data)

            self.speak_dialog('install.complete',
                              dict(skill=self.clean_name(skill)))

    @intent_file_handler('install.beta.intent')
    def install_beta(self, message):
        with self.handle_msm_errors(message.data['skill'], self.remove_word):
            skill = self.find_skill(message.data['skill'], False)
            skill.sha = None
            skills_data = SkillManager.load_skills_data()
            skill_data = skills_data.setdefault(skill.name, {})
            was_beta = skill_data.get('beta')

            if was_beta and skill.is_local:
                self.speak_dialog('error.already.beta',
                                  dict(skill=self.clean_name(skill)))
                return

            if skill.is_local:
                dialog = 'install.beta.upgrade.confirm'
            else:
                dialog = 'install.beta.confirm'

            if not self.confirm_skill_action(skill, dialog):
                return

            if skill.is_local:
                skill.update()
            else:
                skill.install()

            skill_data['beta'] = True
            skill_data['name'] = skill.name
            skill_data['origin'] = 'voice'
            skill_data['installation'] = 'installed'
            skill_data['installed'] = time.time()
            skill_data['failure-message'] = ''
            SkillManager.write_skills_data(skills_data)

            self.speak_dialog('install.beta.complete',
                              dict(skill=self.clean_name(skill)))

    @intent_file_handler('remove.intent')
    def remove(self, message):
        with self.handle_msm_errors(message.data['skill'], self.remove_word):
            skill = self.find_skill(message.data['skill'], True)
            if not skill.is_local:
                raise AlreadyRemoved(skill.name)

            if not self.confirm_skill_action(skill, 'remove.confirm'):
                return

            skill.remove()
            skills_data = SkillManager.load_skills_data()
            if skill.name in skills_data:
                del skills_data[skill.name]
            SkillManager.write_skills_data(skills_data)
            self.speak_dialog('remove.complete',
                              dict(skill=self.clean_name(skill)))

    @intent_file_handler('list.skills.intent')
    def handle_list_skills(self, message):
        skills = [skill for skill in self.msm.list() if not skill.is_local]
        shuffle(skills)
        skills = '. '.join(self.clean_name(skill) for skill in skills[:4])
        skills = skills.replace('skill', '').replace('-',' ')
        self.speak_dialog('some.available.skills', dict(skills=skills))

    @intent_file_handler('install.custom.intent')
    def install_custom(self, message):
        link = self.settings.get('installer_link')
        if link:
            repo_name = SkillEntry.extract_repo_name(link)
            with self.handle_msm_errors(repo_name, self.install_word):
                self.msm.install(link)

    @contextmanager
    def handle_msm_errors(self, repo_name, action):
        try:
            yield
        except MsmException as e:
            self.log.error('MSM failed: ' + repr(e))
            if isinstance(e, (SkillNotFound, AlreadyRemoved, AlreadyInstalled)):
                # A valid skill name is sent as the Exception data (passed in to
                # the constructor) for these Exceptions.  The repo_name passed
                # in was a user-spoken name and is likely inexact.
                skill_name = self.clean_repo_name(str(e))
            else:
                skill_name = repo_name

            error_dialog = {
                SkillNotFound: 'error.not.found',
                SkillRequirementsException: 'error.skill.requirements',
                PipRequirementsException: 'error.pip.requirements',
                SystemRequirementsException: 'error.system.requirements',
                CloneException: 'error.filesystem',
                GitException: 'error.filesystem',
                AlreadyRemoved: 'error.already.removed',
                AlreadyInstalled: 'error.already.installed',
                MultipleSkillMatches: 'error.multiple.skills'
            }.get(type(e), 'error.other')
            self.speak_dialog(error_dialog,
                              data={'skill': skill_name, 'action': action})
        except StopIteration:
            self.speak_dialog('cancelled')

    def on_web_settings_change(self):
        s = self.settings
        link = s.get('installer_link')
        prev_link = s.get('previous_link')
        auto_install = s.get('auto_install')

        # Check if we should auto-install a skill due to web setting change
        if link and prev_link != link and auto_install:
            s['previous_link'] = link

            self.log.info('Installing from the web...')
            action = self.translate_list('action')[0]
            name = SkillEntry.extract_repo_name(link)
            with self.handle_msm_errors(name, action):
                self.msm.install(link)

        # Check for Marketplace updates
        to_install = s.get('to_install', [])
        to_remove = s.get('to_remove', [])

        # Work around backend sending this as json string
        if isinstance(to_install, str):
            to_install = json.loads(to_install)
        if isinstance(to_remove, str):
            to_remove = json.loads(to_remove)

        # If skill exists both in to_install and to_remove don't try
        # to install it.
        removing = [e['name'] for e in to_remove]
        to_install = [e for e in to_install if e['name'] not in removing]
        self.handle_marketplace(to_install, to_remove)

    def handle_marketplace(self, to_install, to_remove):
        skills_data = SkillManager.load_skills_data()
        self.log.info('to_install: {}'.format(to_install))
        installed, failed = self.__marketplace_install(to_install)
        for skill in installed:
            skill_data = skills_data.setdefault(skill, {})
            skill_data['origin'] = 'marketplace'
            skill_data['installation'] = 'installed'
            skill_data['installed'] = time.time()
            skill_data['failure-message'] = ''
            skill_data['updated'] = 0
        for skill in failed:
            skill_data = skills_data.setdefault(skill, {})
            skill_data['origin'] = 'marketplace'
            skill_data['installation'] = 'failed'
            skill_data['installed'] = 0
            skill_data['failure-message'] = 'MsmException occured'
            skill_data['updated'] = 0

        removed = self.__marketplace_remove(to_remove)
        for skill in removed:
            if skill in skills_data:
                del skills_data[skill]

        SkillManager.write_skills_data(skills_data)

    def __filter_by_uuid(self, skills):
        """ Return only skills intended for this device.

        Keeps entrys where the devices field is None of contains the uuid
        of the current device.

        Arguments:
            skills: skill list from to_install or to_remove

        Returns:
            filtered list
        """
        uuid = DeviceApi().get()['uuid']
        return [s for s in skills
                if not s.get('devices') or uuid in s.get('devices')]

    def __marketplace_install(self, install_list):
        try:
            install_list = self.__filter_by_uuid(install_list)
            # Split skill name from author
            skills = [s['name'].split('.')[0] for s in install_list]

            msm_skills = self.msm.list()
            # Remove skills not known to msm
            skills = [s for s in skills if s in [s.name for s in msm_skills]]
            # Remove already installed skills from skills to install
            installed_skills = [s.name for s in msm_skills if s.is_local]
            skills = [s for s in skills if s not in installed_skills]

            self.log.info('Will install {} from the marketplace'.format(skills))

            successes = []
            fails = []
            def install(name):
                s = self.msm.find_skill(name)
                try:
                    s.install()
                    successes.append(name)
                except MsmException as e:
                    self.log.error('{} Could not be installed '
                                   'due to {}'.format(name, repr(e)))
                    fails.append(name)

            result = self.msm.apply(install, skills)
            return successes, fails

        except Exception as e:
            self.log.exception('An error occured installing from marketplace '
                           '({}'.format(repr(e)))
            return [], []

    def __marketplace_remove(self, remove_list):
        try:
            remove_list = self.__filter_by_uuid(remove_list)

            # Split skill name from author
            skills = [skill['name'].split('.')[0] for skill in remove_list]
            self.log.info('Will remove {} from the marketplace'.format(skills))
            # Remove not installed skills from skills to remove
            installed_skills = [s.name for s in self.msm.list() if s.is_local]
            skills = [s for s in skills if s in installed_skills]

            self.log.info('Will remove {} from the marketplace'.format(skills))
            result = self.msm.apply(self.msm.remove, skills)
            return skills
        except Exception as e:
            self.log.error('An error occured installing from marketplace '
                           '({}'.format(repr(e)))
            return []

    def clean_author(self, skill):
        # TODO: Retrieve and use author from skill-data.json
        if skill.author == "mycroftai":
            return "Mycroft AI"  # totally cheating, I know
        else:
            return skill.author

    def clean_repo_name(self, repo):
        name = repo.replace('skill', '').replace('fallback', '').replace('-',' ').strip()
        return name or repo

    def clean_name(self, skill):
        # TODO: Retrieve and use skill-data.json name instead of repo names
        return self.clean_repo_name(skill.name)

    def confirm_skill_action(self, skill, confirm_dialog):
        resp = self.ask_yesno(confirm_dialog,
                              data={'skill': self.clean_name(skill),
                                    'author': self.clean_author(skill)})
        if resp == 'yes':
            return True
        else:
            self.speak_dialog('cancelled')
            return False

    def find_skill(self, param, local):
        """Find a skill, asking if multiple are found"""
        try:
            return self.msm.find_skill(param)
        except MultipleSkillMatches as e:
            skills = [i for i in e.skills if i.is_local == local]
            or_word = self.translate('or')
            if len(skills) >= 10:
                self.speak_dialog('error.too.many.skills')
                raise StopIteration
            names = [self.clean_name(skill) for skill in skills]
            if names:
                response = self.get_response(
                    'choose.skill', num_retries=0,
                    data={'skills': ' '.join([
                        ', '.join(names[:-1]), or_word, names[-1]
                    ])},
                )
                if not response:
                    raise StopIteration
                return self.msm.find_skill(response, skills=skills)
            else:
                raise SkillNotFound(param)


def create_skill():
    return SkillInstallerSkill()
