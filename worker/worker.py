import requests
import concurrent.futures
import sys, os, time, subprocess, psutil

# Worker constants
import socket
import multiprocessing
N_CPUS   = multiprocessing.cpu_count()
HOSTNAME = socket.gethostname()
SERVER   = "https://cachebot.csail.mit.edu"

class Worker:
    def __init__(self, worker_id, hostname):
        self.worker_id = worker_id
        self.hostname  = hostname

        self.worker_params = dict(worker_id = worker_id, hostname = hostname)

    def run_job(self, job):
        """This functions deals with all the logistics of actually running a job"""
        job_id = job["job_id"]

        # Named args need pasting and splitting
        baseline = ["singularity", "run", "netsim.sif"]
        args = " ".join(["--%s %s" % (k, v) for k, v in job["params"].items()]).split()

        # Run it
        proc = subprocess.Popen(baseline + args, stdout = subprocess.DEVNULL, stderr = subprocess.PIPE)
        #proc = subprocess.Popen(["sleep", "5"])

        # Get some info, will be useful later
        pid = proc.pid
        ps  = psutil.Process(pid)
        print("%s: [%d] {%s}" % (self.worker_id, pid, job_id) + " ".join(baseline+args))

        # Wait and do check-ins
        timeout = 2
        memory_usage = 0
        while True:
            try:
                r = proc.wait(timeout = timeout)
                err = proc.stderr.read()
                print("%s: [%d] {%s} done, return code %s\n%s" % (self.worker_id, pid, job_id, r, err.decode("utf-8")))
                if r != 0:
                    print("%s: [%d] {%s} done, return code %s\n%s" % (self.worker_id, pid, job_id, r, err.decode("utf-8")),
                            file = sys.stderr)
                break
            except subprocess.TimeoutExpired:
                pass # Ignore the exception
            except:
                print("???")
                break

            try:
                children = ps.children(recursive = True)
                if children:
                    ps = children[-1]
                memory_usage = ps.memory_info().rss
            except:
                pass
            #print(ps.memory_full_info())
            resp = requests.post(
                    SERVER + "/check-in",
                    data = dict(**self.worker_params,
                                job_id = job["job_id"],
                                memory = memory_usage)
                        )
            try:
                if "wait" in resp.json():
                    timeout = float(resp.json()["wait"])
            except:
                if resp.text != "OK":
                    print("%s: Server responded %s, exiting" % (self.worker_id, resp.text))
                    try:
                        proc.kill()
                    except:
                        pass
                    return


        # DONE, upload results
        try:
            #output_fn = "simulator/done-%s.csv" % job_id
            #resp = requests.post(SERVER + "/start-upload",
            #        data = dict(**self.worker_params))
            #if resp.text != 'OK':
            #    print("Server responded weird to upload...\n%s" % resp.text)
            #    return

            #with open(output_fn) as out_f:
            resp = requests.post(
                    SERVER + "/job-done",
                    data = dict(**self.worker_params,
                                return_code = r,
                                err = err),
                    #files = dict(result = out_f)
                    )

            if resp.text != "OK":
                print(resp.text)
                print("Server responded something weird... stopping job\nServer response:\n%s" % resp.text)
                return
        except Exception as e:
            print(e)
            # Something failed, regardless, don't do anything
            # server might assign us another job, but it'll detect the failure
            print("something weird happened, last server response:\n%s" % resp.text)
            pass



    def start(self):
        """This function tries to get a job, when it does, goes to run"""
        print("Starting worker %s" % self.worker_id)
        while True:
            # Get job
            try:
                response = requests.post(SERVER + "/get-job",
                        data = self.worker_params)
            except:
                # Server not up, something
                time.sleep(10)
                continue

            if response.status_code == 200 and "job" in response.json():
                # Got a job, do it
                job = response.json()["job"]
                self.run_job(job)
                time.sleep(3)
            else:
                # Wait and try again
                try:
                    wait_time = float(response.json()["wait"])
                    time.sleep(wait_time)
                except:
                    time.sleep(5)

            # This guarantees we at least wait 1s between requests
            time.sleep(1)

def local_coordinator(max_jobs):
    """Starts local workers and catches them when they fail"""
    workers = [Worker(i, HOSTNAME) for i in range(max_jobs)]
    with concurrent.futures.ProcessPoolExecutor(max_workers = max_jobs) as executor:
        for i, w in enumerate(workers):
            f = executor.submit(workers[i].start)
            time.sleep(.1) # Avoid coordinated start
            f.add_done_callback(worker_exit)

def worker_exit(f):
    """Gets called on a worker terminating, tries to determine the cause and emails out??"""
    print("A worker terminated...")
    print(f.result())

def update_sif(retry_ok=True):
    """This will be run everytime, re-run the singularity def file if need be"""

    # Check that the local .sif and our def match up
    print("check singularity installed... ", end = "", flush = True)
    try:
        p = subprocess.run("singularity --version".split(), stdout = subprocess.DEVNULL)
        if p.returncode == 0:
            print("\033[0;32mOK\033[0;0m")
        else:
            raise Exception
    except:
        print("\033[0;31mFAIL\033[0;0m")
        return False


    print("check netsim.sif... ", end = "", flush = True)
    # TODO find an actual check
    #p = subprocess.run("singularity inspect --deffile netsim.sif | diff -B - netsim.def",
    #        stderr = subprocess.DEVNULL, shell = True)

    # Yup, we're done!
    if os.path.isfile("netsim.sif"):
        print("\033[0;32mOK\033[0;0m")
        return True

    if retry_ok:
        print("\033[0;31mNO\033[0;0m")

    if retry_ok:
        print("      netsim.sif download... ", end = "", flush = True)
        try:
            cmd = "wget %s/static/netsim.sif" % SERVER
            print(cmd)
            subprocess.run(cmd.split())
            print("\033[0;32mOK, retry\033[0;0m")
            return update_sif(retry_ok = False) # try again, but don't recurse
        except Exception as e:
            print(e)


    print("\033[0;31mFAIL\033[0;0m")
    return False

def check_install(sim_dir = "/home/nibr/sim-worker/"):
    """Make sure everything works, or die trying"""
    # Check if this is a directory first, clone if need be
    # TODO Make this not username depndent
    print("check repo... ", end = "", flush = True)
    if not os.path.isdir(sim_dir):
        print("\033[0;31mNO\033[0;0m")
        print("      repo clone... ", end = "", flush=True)
        subprocess.run(["git", "clone", "https://github.com/nibrivia/rotorsim", sim_dir],
                stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL)
    print("\033[0;32mOK\033[0;0m")

    os.chdir(sim_dir)

    # Check .sif
    if not update_sif():
        return False

    return True

if __name__ == "__main__":
    if not check_install():
        print("setup didn't check out, aborting")
        sys.exit(-1)

    local_coordinator(max_jobs = N_CPUS-2)
