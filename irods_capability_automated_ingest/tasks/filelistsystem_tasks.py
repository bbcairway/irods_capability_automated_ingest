from ..celery import app, RestartTask
from .. import sync_logging, utils
from ..sync_job import sync_job
from .irods_task import IrodsTask
from ..irods import filesystem
from . import filesystem_tasks

import os
import time
import traceback


@app.task(base=RestartTask)
def filelist_main_task(meta):
    job_name = meta["job_name"]
    restart_queue = meta["restart_queue"]
    interval = meta["interval"]
    meta["root_target_collection"] = meta["target"]

    if interval is not None:
        filelist_main_task.s(meta).apply_async(
            task_id=job_name, queue=restart_queue, countdown=interval
        )

    config = meta["config"]
    logging_config = config["log"]
    logger = sync_logging.get_sync_logger(logging_config)

    try:
        logger.info("***************** filelist restart *****************")
        job = sync_job.from_meta(meta)

        if not job.periodic() or job.done():
            job.reset()
            job.start_time_handle().set_value(time.time())
            meta = meta.copy()
            meta["task"] = "filelist_sync_path"
            meta["queue_name"] = meta["path_queue"]
            utils.enqueue_task(filelist_sync_path, meta)
        else:
            logger.info("tasks exist for this job or worker handling this task is busy")

    except Exception as err:
        logger.error("Unexpected error: " + str(err), traceback=traceback.extract_tb(err.__traceback__))
        raise


@app.task(bind=True, base=IrodsTask)
def filelist_sync_path(self, meta):
    config = meta["config"]
    logging_config = config["log"]
    logger = sync_logging.get_sync_logger(logging_config)

    try:
        file_list_path = meta.get("reg_file")
        target_coll = meta["target"]

        if not file_list_path or not os.path.exists(file_list_path):
            logger.error(f"Mapping file not found: {file_list_path}")
            return

        logger.info(f"Reading file list: {file_list_path}")

        meta = meta.copy()
        meta["task"] = "filelist_sync_dir"
        chunk = {}

        files_per_task = meta.get("files_per_task", 50)

        with open(file_list_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'): continue

                parts = line.split(',')
                if len(parts) < 2:
                    logger.warning(f"格式錯誤 (找不到逗號): {line}")
                    continue

                src_filename = parts[0].strip()
                dest_filename = parts[1].strip()

                physical_path = os.path.join(meta["path"], src_filename)
                target_path = os.path.join(target_coll, dest_filename)

                if not os.path.exists(physical_path):
                    logger.warning(f"跳過！硬碟上找不到實體檔案: {physical_path}")
                    continue

                try:
                    stat_res = os.stat(physical_path)
                    obj_stats = {
                        "is_link": False,
                        "is_socket": False,
                        "mtime": stat_res.st_mtime,
                        "ctime": stat_res.st_ctime,
                        "size": stat_res.st_size,
                        "is_empty_dir": False
                    }

                    chunk[physical_path] = {
                        "stats": obj_stats,
                        "target": target_path,
                        "root": physical_path
                    }
                except Exception as e:
                    logger.warning(f"Stat failed for {physical_path}: {e}")
                    continue

                if len(chunk) >= files_per_task:
                    logger.warning(f"ready to send {len(chunk)} data tasks")
                    sync_files_meta = meta.copy()
                    sync_files_meta["chunk"] = chunk
                    sync_files_meta["queue_name"] = meta["file_queue"]
                    utils.enqueue_task(filelist_sync_files, sync_files_meta)
                    chunk = {} 

        if len(chunk) > 0:
            logger.warning(f"ready to send last {len(chunk)} tasks!")
            sync_files_meta = meta.copy()
            sync_files_meta["chunk"] = chunk
            sync_files_meta["queue_name"] = meta["file_queue"]
            utils.enqueue_task(filelist_sync_files, sync_files_meta)
            chunk = {}

    except Exception as err:
        logger.error("Unexpected error in filelist_sync_path: " + str(err))
        raise


@app.task(bind=True, base=IrodsTask)
def filelist_sync_files(self, meta_input):
    meta = meta_input.copy()
    meta["entry_type"] = "file"
    meta["task"] = "sync_file"

    for path, mapping_data in meta["chunk"].items():
        meta["path"] = path
        meta["target"] = mapping_data["target"]
        meta["root"] = mapping_data["root"]

        obj_stats = mapping_data["stats"]
        meta["is_empty_dir"] = obj_stats.get("is_empty_dir")
        meta["is_link"] = obj_stats.get("is_link")
        meta["is_socket"] = obj_stats.get("is_socket")
        meta["mtime"] = obj_stats.get("mtime")
        meta["ctime"] = obj_stats.get("ctime")
        meta["size"] = obj_stats.get("size")

        filesystem_tasks.filesystem_sync_entry(
            self,
            meta,
            filesystem.sync_data_from_file,
            filesystem.sync_metadata_from_file,
        )
