"""
OpenShift cluster manager module that provides functionality to schedule jobs as well as
manage their state in the cluster.
"""

import yaml
import shlex
import os
from kubernetes import client as k_client, config
from kubernetes.client.rest import ApiException
from .abstractmgr import AbstractManager, ManagerException, JobInfo, JobStatus, TimeStamp

class OpenShiftManager(AbstractManager):

    def __init__(self, config_dict=None):
        super().__init__(config_dict)
        self.kube_client = None
        self.kube_v1_batch_client = None
        self.project = os.environ.get('OPENSHIFTMGR_PROJECT') or 'myproject'

        # init the openshift client
        self.init_openshift_client()

    def init_openshift_client(self):
        """
        Method to get a OpenShift client connected to remote or local OpenShift
        """
        kubecfg_path = os.environ.get('KUBECFG_PATH')
        if kubecfg_path is None:
            config.load_kube_config()
        else:
            config.load_kube_config(config_file=kubecfg_path)
        self.kube_client = k_client.CoreV1Api()
        self.kube_v1_batch_client = k_client.BatchV1Api()

    # The kubecfg secret being mounted in the third container (publish) is the one that is generated by the service account
    def schedule_job(self, image, command, name, resource_dict, share_dir):
        """
        Schedule a new job and returns the job object.
        """
        command = command.replace("/share",share_dir)
        number_of_workers = str(resource_dict['number_of_workers'])
        memory_limit = str(resource_dict['memory_limit'])+ 'Mi'
        cpu_limit = str(resource_dict['cpu_limit']) + 'm'
        gpu_limit = str(resource_dict['gpu_limit'])
        incoming_dir='/share/incoming'
        outgoing_dir='/share/outgoing'
        d_job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": name
            },
            "spec": {
                "ttlSecondsAfterFinished": 20,
                "parallelism": number_of_workers,
                "completions": number_of_workers,
                "activeDeadlineSeconds": 36000,
                "template": {
                    "metadata": {
                         "labels":{
                            "job-origin":"pman"
                        },
                        "name": name
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "env": [
                                    {
                                        "name": "NUMBER_OF_WORKERS",
                                        "value": number_of_workers
                                    },
                                    {
                                        "name": "CPU_LIMIT",
                                        "value": cpu_limit
                                    },
                                    {
                                        "name": "MEMORY_LIMIT",
                                        "value": memory_limit
                                    }
                                ],
                                "name": name,
                                "image": image,
                                "imagePullSecrets":"regcred",
                                "imagePullPolicy": "IfNotPresent",
                                "command": shlex.split(command),
                                "resources": {
                                    "limits": {
                                        "memory": memory_limit,
                                        "cpu": cpu_limit
                                    },
                                    "requests": {
                                        "memory": "150Mi",
                                        "cpu": "250m"
                                    }
                                },
                                "volumeMounts": [
                                    {
                                        "mountPath": "/tmp/",
                                        "name": "gluster-vol1"
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        }
        if int(gpu_limit) > 0:  # Typecasting before a check
            # The assumption is containers[0] is always image plugin pod as the publish container is appended later.
            # These is specific to 3.9+  Ref: https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/
            d_job['spec']['template']['spec']['containers'][0]['resources']['limits']={}
            d_job['spec']['template']['spec']['containers'][0]['resources']['requests']={}
            d_job['spec']['template']['spec']['containers'][0]['resources']['limits']['nvidia.com/gpu'] = int(gpu_limit)
            # Add node selector for node.
            # d_job['spec']['template']['spec']['nodeSelector'] = {'accelerator': 'gpu-node'}
            d_job['spec']['template']['spec']['containers'][0]['securityContext']= {
                "allowPrivilegeEscalation": False,
						"capabilities": {
							"drop": [
								"ALL"
							]
						},
						"seLinuxOptions":{
						    "type": "nvidia_container_t"}
            }
        
        d_job['spec']['template']['spec']['volumes'] = [
                {
                    "name": "gluster-vol1",
                    "persistentVolumeClaim": {
                        "claimName": "gluster1"
                    }
                },
                {
                    "name": "swift-credentials",
                    "secret": {
                        "secretName": "swift-credentials"
                    }
                },
                {
                    "name": "kubecfg-volume",
                    "secret": {
                        "secretName": "kubecfg"
                    }
                },
                {
                    "mountPath": "/local",
                    "name": "local-volume"
                }
            ]

        job = self.kube_v1_batch_client.create_namespaced_job(namespace=self.project, body=d_job)
        return job

    def get_pod_status(self, name):
        """
        Get a pod's status
        """
        log = self.kube_client.read_namespaced_pod_status(namespace=self.project, name=name)
        return str(log)

    def get_pod_log(self, name, container_name=None):
    
        # Query for pod logs
        # If container is not started
        # send default msg
        try:
            log = self.kube_client.read_namespaced_pod_log(namespace=self.project, name=name)
            return log
        except:
            return (f"Pod {name} is being created. Logs will appear shortly")
       

    def get_job_object(self, name):
        """
        Get the previously scheduled job object
        """
        return self.kube_v1_batch_client.read_namespaced_job(name, self.project)
        
    def get_job_info(self,job) -> JobInfo:
        """
        Get job info from previously scheduled job
        """
        status: JobStatus = JobStatus.notstarted
        message = 'task not available yet'
        conditions = job.status.conditions
        failed = job.status.failed
        succeeded = job.status.succeeded
        completion_time = job.status.completion_time

        if not (conditions is None and failed is None and succeeded is None):
            if conditions:
                for condition in conditions:
                    if condition.type == 'Failed' and condition.status == 'True':
                        message = condition.message
                        status = JobStatus.finishedWithError
                        break
            if status == JobStatus.notstarted:
                if completion_time and succeeded:
                    message = 'finished'
                    status = JobStatus.finishedSuccessfully
                elif job.status.active:
                    message = 'running'
                    status = JobStatus.started
                else:
                    message = 'inactive'
                    status = JobStatus.undefined

        return JobInfo(
            name=job.metadata.name,
            image=job.spec.template.spec.containers[0].image,
            cmd=' '.join(job.spec.template.spec.containers[0].command),
            timestamp=TimeStamp(completion_time.isoformat() if completion_time is not None else ''),
            message=message,
            status=status
        )

    def remove_job(self, job):
        """
        Remove a previously scheduled job
        """
        name = job.metadata.name
        #self.remove_pvc(name)
        body = k_client.V1DeleteOptions(propagation_policy='Background')
        self.kube_v1_batch_client.delete_namespaced_job(name, body=body, namespace=self.project)

    def remove_pod(self, name):
        """
        Remove a previously scheduled pod
        """
        self.kube_client.delete_namespaced_pod(name, self.project, {})

    def get_job(self, name):
        """
        Return the state of a previously scheduled job
        """
        job = self.get_job_object(name)
        return job
        
                                    
    def get_job_logs(self,job):
        #return ''
        name = job.metadata.name
        str_logs = ''
        
        pod_names = self.get_pod_names_in_job(name)
        for _, pod_name in enumerate(pod_names):
            str_logs += self.get_job_pod_logs(pod_name, name)

        return str_logs
        

    def get_job_pod_logs(self, pod_name, jid):
        """
        Returns the concatenated log of all 3 containers part of job template.
        :param str pod_name: job-id of OpenShift job.  
        :return: str log: Combined log of all the containers in pod.
        """
        # Assumption is pod is always going to have a init-storage, container with job-id, publish container, if not we just return log for job container.
        # TODO: @ravig: Think of a better way to abstract out logs in case of multiple pods running parallelly.

        job_container_log = self.get_pod_log(pod_name, jid)
        return job_container_log
        

    def get_pod_names_in_job(self, job_id):
        """
        Returns names of all the pods created as part of job
        :param str job_id: job-id of OpenShift job
        :return: 
        """
        pod_names = []
        # job-name becomes selector based on which we can choose jobs that we created just now.
        pods = self.kube_client.list_namespaced_pod(self.project, label_selector='job-name='+job_id)
        for _, pod_item in enumerate(pods.items):
            pod_names.append(pod_item.metadata.name)
        return pod_names

    
    def remove_pvc(self, job_id):
        """
        Remove pvc created
        """
        pvc_name = job_id+"-storage-claim"
        body = k_client.V1DeleteOptions()
        self.kube_client.delete_namespaced_persistent_volume_claim(pvc_name, self.project, body=body)
