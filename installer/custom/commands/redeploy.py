from core.commands import BaseCommand
from core.config import Settings
from core import constants as K
from core.terraform.resources.aws.ecs import ECSTaskDefinitionResource, ECSClusterResource
from core.terraform import PyTerraform
from core.providers.aws.boto3.ecs import stop_all_tasks_in_a_cluster, deregister_task_definition
from threading import Thread
import time
import importlib
import sys
import inspect
import os


class Redeploy(BaseCommand):
    """
    This calss is defined to redeploy PacBot which is already installed by Installer command

    Attributes:
        validation_class (class): This validate the input and resources
        input_class (class): Main class to read input from user
        install_class (class): Provider based install class
        need_complete_install (boolean): True if complete installation is required else False

    """
    def __init__(self, args):
        args.append((K.CATEGORY_FIELD_NAME, "deploy"))
        self.need_complete_install = self._need_complete_installation()
        Settings.set('SKIP_RESOURCE_EXISTENCE_CHECK', True)
        super().__init__(args)

    def _need_complete_installation(self):
        need_complete_install = False

        redshift_cluster_file = os.path.join(Settings.TERRAFORM_DIR, "datastore_redshift_RedshiftCluster.tf")
        if os.path.exists(redshift_cluster_file):
            need_complete_install = True

        return need_complete_install

    def execute(self, provider):
        """
        Command execution starting point

        Args:
            provider (string): Provider name like AWS or Azure etc
        """
        self.initialize_install_classes(provider)

        if self.check_pre_requisites() is False:
            self.exit_system_with_pre_requisites_fail()

        input_instance = self.read_input()
        self.re_deploy_pacbot(input_instance)

    def initialize_install_classes(self, provider):
        """
        Initialise classes based on the provider

        Args:
            provider (string): Provider name like AWS or Azure etc
        """
        self.validation_class = getattr(importlib.import_module(
            provider.provider_module + '.validate'), 'SystemInstallValidation')
        self.input_class = getattr(importlib.import_module(
            provider.provider_module + '.input'), 'SystemInstallInput')
        self.install_class = getattr(importlib.import_module(
            provider.provider_module + '.install'), 'Install')

    def re_deploy_pacbot(self, input_instance):
        """
        Start method for redeploy

        Args:
            input_instance (Input object): User input values
        """
        resources_to_taint = self.get_resources_to_process(input_instance)
        try:
            response = PyTerraform().terraform_taint(resources_to_taint)  # If tainted or destroyed already then skip it
        except:
            pass

        terraform_with_targets = False if self.need_complete_install else True
        resources_to_process = self.get_complete_resources(input_instance) if self.need_complete_install else resources_to_taint

        self.run_real_deployment(input_instance, resources_to_process, terraform_with_targets)

    def inactivate_required_services_for_redeploy(self, resources_to_process, dry_run):
        """
        Before redeploy get started or on redeploy happens stop the tasks and deregister task definition

        Args:
            resources_to_process (list): List of resources to be created/updated
            only_tasks (boolean): This flasg decides whther to deregister task definition or not
        """
        if dry_run:
            return

        for resource in resources_to_process:
            if self.terraform_thread.isAlive():
                resource_base_classes = inspect.getmro(resource.__class__)

                if ECSTaskDefinitionResource in resource_base_classes:
                    try:
                        deregister_task_definition(
                            Settings.AWS_ACCESS_KEY,
                            Settings.AWS_SECRET_KEY,
                            Settings.AWS_REGION,
                            resource.get_input_attr('family'),
                        )
                    except:
                        pass
                elif ECSClusterResource in resource_base_classes:
                    cluster_name = resource.get_input_attr('name')
            else:
                return

        for i in range(3):
            if self.terraform_thread.isAlive():
                try:
                    stop_all_tasks_in_a_cluster(
                        cluster_name,
                        Settings.AWS_ACCESS_KEY,
                        Settings.AWS_SECRET_KEY,
                        Settings.AWS_REGION
                    )
                except:
                    pass
                time.sleep(20)
            else:
                return

    def run_real_deployment(self, input_instance, resources_to_process, terraform_with_targets):
        """
        Main thread method which invokes the 2 thread: one for actual execution and another for displaying status

        Args:
            input_instance (Input obj): Input object with values read from user
            resources_to_process (list): List of resources to be created/updated
            terraform_with_targets (boolean): This is True since redeployment is happening
        """
        self.terraform_thread = Thread(target=self.run_tf_apply, args=(input_instance, list(resources_to_process), terraform_with_targets))
        stop_related_task_thread = Thread(target=self.inactivate_required_services_for_redeploy, args=(list(resources_to_process), self.dry_run))

        self.terraform_thread.start()
        stop_related_task_thread.start()

        self.terraform_thread.join()
        stop_related_task_thread.join()

    def run_tf_apply(self, input_instance, resources_to_process, terraform_with_targets):
        """
        Execute the installation of resources by invoking the execute method of provider class

        Args:
            input_instance (Input obj): Input object with values read from user
            resources_to_process (list): List of resources to be created/updated
            terraform_with_targets (boolean): This is True since redeployment is happening
        """
        self.install_class(
            self.args,
            input_instance,
            check_dependent_resources=False
        ).execute(
            resources_to_process,
            terraform_with_targets,
            self.dry_run
        )
