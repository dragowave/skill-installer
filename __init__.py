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

from msm import SkillNotFound, SkillRequirementsException, \
    PipRequirementsException, SystemRequirementsException, CloneException, \
    GitException, AlreadyRemoved, AlreadyInstalled, MsmException, SkillEntry, \
    MultipleSkillMatches, MycroftSkillsManager
from mycroft import intent_file_handler, MycroftSkill
from mycroft.api import DeviceApi
from mycroft.skills.skill_manager import SkillManager

def get_entry(skills_data, name):
    for entry in skills_data['skills']:
        if entry.get('name') == name:
            break
    else:
        entry = {}
    return entry


class SkillInstallerSkill(MycroftSkill):
    def __init__(self):
        super(SkillInstallerSkill, self).__init__()
        try:
            self.msm = SkillManager.create_msm()
            self.thread_lock = SkillManager.get_lock()
        except AttributeError:
            self.msm = MycroftSkillsManager()
        self.yes_words = self.install_word = self.remove_word = None

    def initialize(self):
        self.settings.set_changed_callback(self.on_web_settings_change)
        self.install_word, self.remove_word = self.translate_list('action')
        self.yes_words = set(self.translate_list('yes'))

    @intent_file_handler('install.intent')
    def install(self, message):
        # Failsafe if padatious matches without skill entity.
        if not message.data.get('skill'):
            return self.handle_list_skills(message)

        with self.handle_msm_errors(message.data['skill'], self.remove_word):
            skill = self.find_skill(message.data['skill'], False)
            with self.msm.lock, self.thread_lock:
                self.msm.sync_skills_data()
                entry = get_entry(self.msm.skills_data, skill.name)
                was_beta = entry.get('beta')

                if not was_beta and skill.is_local:
                    raise AlreadyInstalled(skill.name)

                if skill.is_local:
                    dialog = 'install.reinstall.confirm'
                else:
                    dialog = 'install.confirm'
                if not self.confirm_skill_action(skill, dialog):
                    return

                if skill.is_local:
                    self.msm.remove(skill.name)
                    skill.is_local = False # ?
                self.msm.install(skill, origin='voice')
                self.msm.write_skills_data()
            self.upload_skills_data(self.msm.skills_data)

            self.speak_dialog('install.complete', dict(skill=skill.name))

    @intent_file_handler('install.beta.intent')
    def install_beta(self, message):
        with self.handle_msm_errors(message.data['skill'], self.remove_word):
            skill = self.find_skill(message.data['skill'], False)
            skill.sha = None # None -> Will fetch latest (beta) version
            with self.msm.lock, self.thread_lock:
                self.msm.sync_skills_data()
                entry = get_entry(self.msm.skills_data, self.name)
                was_beta = entry.get('beta')

                if was_beta and skill.is_local:
                    self.speak_dialog('error.already.beta',
                                      dict(skill=skill.name))
                    return

                if skill.is_local:
                    dialog = 'install.beta.upgrade.confirm'
                else:
                    dialog = 'install.beta.confirm'

                if not self.confirm_skill_action(skill, dialog):
                    return

                if skill.is_local:
                    self.msm.update(skill)
                else:
                    self.msm.install(skill, origin='voice')
                self.msm.write_skills_data()
            self.upload_skills_data(self.msm.skills_data)

            self.speak_dialog('install.beta.complete', dict(skill=skill.name))

    @intent_file_handler('remove.intent')
    def remove(self, message):
        with self.handle_msm_errors(message.data['skill'], self.remove_word):
            skill = self.find_skill(message.data['skill'], True)
            if not skill.is_local:
                raise AlreadyRemoved(skill.name)

            if not self.confirm_skill_action(skill, 'remove.confirm'):
                return

            with self.msm.lock, self.thread_lock:
                self.msm.sync_skills_data()
                self.msm.remove(skill)
                self.msm.write_skills_data(self.msm.skills_data)
            self.speak_dialog('remove.complete', dict(skill=skill.name))
            self.upload_skills_data(self.msm.skills_data)

    @intent_file_handler('list.skills.intent')
    def handle_list_skills(self, message):
        skills = [skill for skill in self.msm.list() if not skill.is_local]
        shuffle(skills)
        skills = ', '.join(skill.name for skill in skills[:4])
        self.speak_dialog('some.available.skills', dict(skills=skills))

    @intent_file_handler('install.custom.intent')
    def install_custom(self, message):
        link = self.settings.get('installer_link')
        if link:
            name = SkillEntry.extract_repo_name(link)
            with self.handle_msm_errors(name, self.install_word):
                self.msm.install(link)

    @contextmanager
    def handle_msm_errors(self, skill, action):
        try:
            yield
        except MsmException as e:
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
            data = {'skill': skill, 'action': action}
            if isinstance(e, (SkillNotFound, AlreadyRemoved,
                              AlreadyInstalled)):
                data['skill'] = str(e)
            self.speak_dialog(error_dialog, data)
            self.log.error('Msm failed: ' + repr(e))
        except StopIteration:
            self.speak_dialog('cancelled')

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
        return [s for s in skills if not s['devices'] or uuid in s['devices']]

    def __marketplace_install(self, install_list):
        if isinstance(install_list, str):
            install_list = json.loads(install_list)
        install_list = self.__filter_by_uuid(install_list)
        # Split skill name from author
        skills = [s['name'].split('.')[0] for s in install_list]

        self.log.info('Will install {} from the marketplace'.format(skills))
        # Remove already installed skills from skills to install
        installed_skills = [s.name for s in self.msm.list() if s.is_local]
        skills = [s for s in skills if s not in installed_skills]

        self.log.info('Will install {} from the marketplace'.format(skills))

        def install(skill):
            self.msm.install(skill, origin='marketplace')

        result = self.msm.apply(install, skills)

    def __marketplace_remove(self, remove_list):
        # Work around backend sending this as json string
        if isinstance(remove_list, str):
            remove_list = json.loads(remove_list)

        remove_list = self.__filter_by_uuid(remove_list)

        # Split skill name from author
        skills = [skill['name'].split('.')[0] for skill in remove_list]
        self.log.info('Will remove {} from the marketplace'.format(skills))
        # Remove not installed skills from skills to remove
        installed_skills = [s.name for s in self.msm.list() if s.is_local]
        skills = [s for s in skills if s in installed_skills]

        self.log.info('Will remove {} from the marketplace'.format(skills))
        result = self.msm.apply(self.msm.remove, skills)

    def on_web_settings_change(self):
        self.log.info('Installer Skill web settings have changed')
        s = self.settings
        link = s.get('installer_link')
        prev_link = s.get('previous_link')
        auto_install = s.get('auto_install') == 'true'

        if link and prev_link != link and auto_install:
            s['previous_link'] = link

            action = self.translate_list('action')[0]
            name = SkillEntry.extract_repo_name(link)
            with self.handle_msm_errors(name, action):
                self.msm.install(link)

        with self.msm.lock, self.thread_lock:
            self.msm.sync_skills_data()
            self.__marketplace_install(to_install)
            self.__marketplace_remove(to_remove)
            self.msm.write_skills_data()
        self.upload_skills_data(self.msm.skills_data)

    def confirm_skill_action(self, skill, confirm_dialog):
        response = self.get_response(
            confirm_dialog, num_retries=0,
            data={'skill': skill.name, 'author': skill.author}
        )

        if response and self.yes_words & set(response.split()):
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
            names = [skill.name for skill in skills]
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

    def upload_skills_data(self, skills_data):
        if self.config_core['skills'].get('upload_skill_manifest', False):
            try:
                DeviceApi().upload_skills_data(skills_data)
            except Exception as e:
                self.log.error('An exception occured while uploading the '
                               ' skills manifest ({})'.format(repr(e)))

    def stop(self):
        pass


def create_skill():
    return SkillInstallerSkill()
