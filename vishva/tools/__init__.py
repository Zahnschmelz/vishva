from . import (bash, cd, bg_task, write_file, read_file, read_cache, edit_file, list_dir,
               sys_update, sys_clean, web_read, web_search, product_search,
               speak, sched_task, spotify, vol_ctl, turnoff_screen, subagent,
               weather, news_digest, lint_code, send_image,
               brightness_ctl, send_file, rag_tools)

MODULES = [bash, cd, bg_task, write_file, read_file, read_cache, edit_file, list_dir,
           sys_update, sys_clean, web_read, web_search, product_search,
           speak, sched_task, spotify, vol_ctl, turnoff_screen, subagent,
           weather, news_digest, lint_code, send_image,
           brightness_ctl, send_file, rag_tools]


def bind_tools(cls):
    for mod in MODULES:
        for name in dir(mod):
            if name.startswith("_") and not name.startswith("__"):
                fn = getattr(mod, name)
                if callable(fn):
                    setattr(cls, name, fn)
