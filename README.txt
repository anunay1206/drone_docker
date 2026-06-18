========================================================================
 Tree-Crown Species Pipeline — run from Docker Hub
========================================================================

This bundle runs a backend (FastAPI) + a simple web UI as Docker
containers. The container images are hosted on Docker Hub:

    uavforaliens/treecrown-workstation : latest   (backend API)
    uavforaliens/treecrown-frontend    : latest   (web UI)

The images contain the Python environment + all dependencies. The
application code, the data/output folder, and the model weights are
provided from THIS folder at run time (bind-mounted), so keep this whole
folder together.

------------------------------------------------------------------------
 0. WHAT YOU NEED ON THIS PC
------------------------------------------------------------------------
  - Docker (Engine + "docker compose" v2, or Docker Desktop).
  - This folder, containing:
        docker-compose.hub.yml
        .env.example
        code/        (the application code)
        data/        (empty; results + database are written here)
        models/      (you place the detector weights here)
        airflow/dags/ (only needed if you use Airflow)
  - The three detector weight files (.pth) — get these from the
    maintainer; they are NOT on Docker Hub (too large):
        urban_trees_Cambridge_20230630.pth
        220723_withParacouUAV.pth
        230103_randresize_full.pth
  - Internet access the first time (to download the images, and to fetch
    the DINOv2 feature model once on the first analysis).

------------------------------------------------------------------------
 1. INSTALL DOCKER (skip if already installed)
------------------------------------------------------------------------
  Windows / Mac : install "Docker Desktop" and start it.
  Linux         : install Docker Engine + the compose plugin, then:
                      sudo service docker start
                      sudo usermod -aG docker $USER   (then re-open shell)
  Verify:
      docker run --rm hello-world

------------------------------------------------------------------------
 2. DOWNLOAD THE IMAGES FROM DOCKER HUB
------------------------------------------------------------------------
  If the repositories are PRIVATE, log in first:
      docker login

  Pull both images:
      docker pull uavforaliens/treecrown-workstation:latest
      docker pull uavforaliens/treecrown-frontend:latest

  (You can skip the manual pull — step 5 pulls them automatically.)

------------------------------------------------------------------------
 3. CONFIGURE  (.env)
------------------------------------------------------------------------
  Create the .env file from the template:
      cp .env.example .env

  Make sure these two lines are present (which images to run):
      IMAGE_API=uavforaliens/treecrown-workstation:latest
      IMAGE_FRONTEND=uavforaliens/treecrown-frontend:latest

  If you will use Airflow (see step 7), also set:
      TCP_AIRFLOW_BASE_URL=http://host.docker.internal:8080
      TCP_AIRFLOW_USERNAME=admin
      TCP_AIRFLOW_PASSWORD=<your airflow admin password>

  To try it WITHOUT Airflow first (recommended), leave
  TCP_AIRFLOW_BASE_URL blank — the backend then runs the pipeline by
  itself and the UI still works.

------------------------------------------------------------------------
 4. ADD THE MODEL WEIGHTS
------------------------------------------------------------------------
  Copy the three .pth files into the models/ folder:
      models/urban_trees_Cambridge_20230630.pth
      models/220723_withParacouUAV.pth
      models/230103_randresize_full.pth

------------------------------------------------------------------------
 5. START IT
------------------------------------------------------------------------
      docker compose -f docker-compose.hub.yml pull
      docker compose -f docker-compose.hub.yml up -d
      docker compose -f docker-compose.hub.yml ps

------------------------------------------------------------------------
 6. USE IT
------------------------------------------------------------------------
  Backend health:
      curl http://localhost:8123/livez            -> {"status":"ok"}
  Confirm the weights are visible:
      docker compose -f docker-compose.hub.yml exec api ls -lh /models
      docker compose -f docker-compose.hub.yml exec api curl -s http://localhost:8123/api/v1/detectors

  Open the web UI in a browser:
      http://localhost:8200
  In the UI, set "API base URL" to:
      http://localhost:8123

  Then: create a project -> upload a GeoTIFF -> Analyze -> Label -> Finalize.
  (The first Analyze pauses a little while it downloads the DINOv2 model.)

  Ports (host side):
      8123  -> backend API
      8200  -> web UI

------------------------------------------------------------------------
 7. AIRFLOW (optional — "full loop" where Airflow triggers the backend)
------------------------------------------------------------------------
  On the machine running Airflow:
    a) Copy the DAGs from airflow/dags/ into your Airflow "dags" folder:
           drone_analyze_dag.py
           drone_finalize_dag.py
    b) Tell the DAGs where THIS backend is (env on the Airflow worker):
           DRONE_API_BASE=http://localhost:8123
       (use http://<this-PC-ip>:8123 if Airflow runs on another machine;
        port 8123 must be reachable from the Airflow machine.)
    c) In this folder's .env, point the backend at Airflow's REST API
       (TCP_AIRFLOW_BASE_URL + username/password), then restart:
           docker compose -f docker-compose.hub.yml up -d

  Check the container can reach a host-local Airflow:
      docker compose -f docker-compose.hub.yml exec api \
          curl -s http://host.docker.internal:8080/health

------------------------------------------------------------------------
 8. MANAGE
------------------------------------------------------------------------
  Logs:           docker compose -f docker-compose.hub.yml logs -f api
  Stop:           docker compose -f docker-compose.hub.yml down
  Update images:  docker compose -f docker-compose.hub.yml pull
                  docker compose -f docker-compose.hub.yml up -d

  Your data (projects, results, database) persists in the data/ folder
  across restarts. Deleting data/ resets everything.

------------------------------------------------------------------------
 9. TROUBLESHOOTING
------------------------------------------------------------------------
  - Detector shows "weights missing" / Analyze fails:
        the .pth files are not in models/.
  - "DISPATCH_FAILED" when starting Analyze:
        TCP_AIRFLOW_BASE_URL / username / password in .env are wrong,
        or Airflow is not running. (Leave the URL blank to run without
        Airflow.)
  - Airflow DAG task fails calling back to the backend:
        DRONE_API_BASE on the Airflow side must be http://localhost:8123
        (or http://<this-PC-ip>:8123), and port 8123 must be reachable.
  - Container cannot reach Airflow:
        check  ...exec api curl http://host.docker.internal:8080/health
  - "permission denied ... docker.sock":
        add your user to the docker group (see step 1) and re-open shell.
  - Port already in use:
        edit docker-compose.hub.yml and change the host side of
        "8123:8000" or "8200:80".

------------------------------------------------------------------------
 NOTE
------------------------------------------------------------------------
  The image carries the dependencies only; the app code lives in code/
  (bind-mounted), which is why this folder must stay together. If you
  want a single pull-and-run image with the code baked in (so only
  models/ + .env are needed), ask the maintainer for the "baked-code"
  build.
