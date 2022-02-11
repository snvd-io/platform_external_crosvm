#!/usr/bin/env python3
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Generates bindings that are used gpu_renderer.

A sysroot and virglrenderer source checkout is required. The defaults to the
root directory.
"""

from __future__ import print_function
import argparse
import multiprocessing.pool
import os
import subprocess
import sys
import tempfile

# Bright green.
PASS_COLOR = '\033[1;32m'
# Bright red.
FAIL_COLOR = '\033[1;31m'
# Default color.
END_COLOR = '\033[0m'

verbose = False

def generate_module(module_name, allowlist, blocklist, header, clang_args,
                    lib_name, derive_default):
  args = [
    'bindgen',
    '--no-layout-tests',
    '--allowlist-function', allowlist,
    '--allowlist-var', allowlist,
    '--allowlist-type', allowlist,
    '--blocklist-function', blocklist,
    '--blocklist-item', blocklist,
    '--blocklist-type', blocklist,
    '--no-prepend-enum-name',
    '-o', module_name + '_bindings.rs',
  ];

  if lib_name:
    args.extend(['--raw-line',
                 '#[cfg(feature = \"{}\")]'.format(module_name)])
    args.extend(['--raw-line',
                 '#[link(name = "{}")] extern {{}}'.format(lib_name)])

  if derive_default:
    args.append('--with-derive-default')

  args.extend([header, '--'])
  args.extend(clang_args)

  if verbose:
    print(' '.join(args))

  if subprocess.Popen(args).wait() == 0:
    return 'pass'
  else:
    return 'bindgen failed'


def download_virgl(src, dst, branch):
  virgl_src = tempfile.TemporaryDirectory(prefix='virglrenderer-src')

  args = ['git', 'clone']

  if branch:
    args.extend(['-b', branch])

  args.extend([src, dst])
  
  if verbose:
    print(' '.join(args))

  if subprocess.Popen(args).wait() == 0:
    return True
  else:
    
    return False


def get_parser():
  """Gets the argument parser"""
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--sysroot',
                      default='/',
                      help='sysroot directory (default=%(default)s)')
  parser.add_argument('--virglrenderer',
                      help='virglrenderer src dir/repo (default=%(default)s)')
  parser.add_argument('--virgl_branch',
                      default='master',
                      help='virglrenderer branch name (default=%(default)s)')
  parser.add_argument('--verbose', '-v',
                      action='store_true',
                      help='enable verbose output (default=%(default)s)')
  return parser


def main(argv):
  global verbose
  os.chdir(os.path.dirname(sys.argv[0]))
  opts = get_parser().parse_args(argv)
  if opts.verbose:
    verbose = True

  if opts.virglrenderer:
    if '://' in opts.virglrenderer:
      virgl_src_dir_temp = tempfile.TemporaryDirectory(prefix='virglrenderer-src')
      virgl_src_dir = virgl_src_dir_temp.name
      if not download_virgl(opts.virglrenderer, virgl_src_dir, opts.virgl_branch):
        print('failed to clone \'{}\' to \'{}\''.format(virgl_src_dir,
                                                        opts.virgl_branch))
        sys.exit(1)
    else:
      virgl_src_dir = opts.virglrenderer

    header = os.path.join(virgl_src_dir, 'src/virglrenderer.h')
  else:
    header = os.path.join(opts.sysroot, 'usr/include/virgl/virglrenderer.h')

  clang_args = ['-I', os.path.join(opts.sysroot, 'usr/include'),
                '-D', 'VIRGL_RENDERER_UNSTABLE_APIS']

  modules = (
    (
      'virgl_renderer',
      '(virgl|VIRGL)_.+', # allowlist
      '.*(va_list|debug_callback).*', # blocklist
      header,
      clang_args,
      'virglrenderer',
      True,
    ),
  )

  pool = multiprocessing.pool.Pool(len(modules))
  results = pool.starmap(generate_module, modules, 1)

  return_fail = False
  print('---')
  print('generate module summary:')
  for module, result in zip(modules, results):
    result_color = FAIL_COLOR
    if result == 'pass':
      result_color = PASS_COLOR
    else:
      return_fail = True

    print('%15s: %s%s%s' %
          (module[0], result_color, result, END_COLOR))

  if return_fail:
    sys.exit(1)

  with open('mod.rs', 'w') as f:
    print('/* generated by generate.py */', file=f)
    print('#![allow(dead_code)]', file=f)
    print('#![allow(non_camel_case_types)]', file=f)
    print('#![allow(non_snake_case)]', file=f)
    print('#![allow(non_upper_case_globals)]', file=f)
    print('pub mod virgl_debug_callback_bindings;', file=f)
    for module in modules:
      print('pub mod', module[0] + '_bindings;', file=f)


if __name__ == '__main__':
  sys.exit(main(sys.argv[1:]))
