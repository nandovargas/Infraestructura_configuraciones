# Nodo de control de Ansible (Ansible no corre como nodo de control en Windows).
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client rsync \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "ansible-core==2.18.6"
COPY requirements.yml /tmp/requirements.yml
RUN ansible-galaxy collection install -r /tmp/requirements.yml -p /usr/share/ansible/collections
WORKDIR /work
