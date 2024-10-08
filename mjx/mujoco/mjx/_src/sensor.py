# Copyright 2023 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Sensor functions."""

import jax
from jax import numpy as jp
import mujoco
# pylint: disable=g-importing-member
from mujoco.mjx._src import math
from mujoco.mjx._src import ray
from mujoco.mjx._src.types import Data
from mujoco.mjx._src.types import DisableBit
from mujoco.mjx._src.types import Model
from mujoco.mjx._src.types import ObjType
from mujoco.mjx._src.types import SensorType
# pylint: enable=g-importing-member
import numpy as np


def sensor_pos(m: Model, d: Data) -> Data:
  """Compute position-dependent sensors values."""

  if m.opt.disableflags & DisableBit.SENSOR:
    return d

  # position and orientation by object type
  objtype_data = {
      ObjType.UNKNOWN: (
          np.expand_dims(np.eye(3), axis=0),
          np.zeros((1, 3)),
      ),  # world
      ObjType.BODY: (d.xipos, d.ximat),
      ObjType.XBODY: (d.xpos, d.xmat),
      ObjType.GEOM: (d.geom_xpos, d.geom_xmat),
      ObjType.SITE: (d.site_xpos, d.site_xmat),
      ObjType.CAMERA: (d.cam_xpos, d.cam_xmat),
  }

  # frame axis indexing
  frame_axis = {
      SensorType.FRAMEXAXIS: 0,
      SensorType.FRAMEYAXIS: 1,
      SensorType.FRAMEZAXIS: 2,
  }

  stage_pos = m.sensor_needstage == mujoco.mjtStage.mjSTAGE_POS
  sensors, adrs = [], []

  for sensor_type in set(m.sensor_type[stage_pos]):
    idx = m.sensor_type == sensor_type
    objid = m.sensor_objid[idx]
    objtype = m.sensor_objtype[idx]
    refid = m.sensor_refid[idx]
    reftype = m.sensor_reftype[idx]
    adr = m.sensor_adr[idx]

    if sensor_type == SensorType.MAGNETOMETER:
      sensor = jax.vmap(lambda xmat: xmat.T @ m.opt.magnetic)(
          d.site_xmat[objid]
      ).reshape(-1)
      adr = (adr[:, None] + np.arange(3)[None]).reshape(-1)
    elif sensor_type == SensorType.CAMPROJECTION:

      @jax.vmap
      def _cam_project(
          target_xpos, xpos, xmat, res, fovy, intrinsic, sensorsize, focal_flag
      ):
        translation = jp.eye(4).at[0:3, 3].set(-xpos)
        rotation = jp.eye(4).at[:3, :3].set(xmat.T)

        # focal transformation matrix (3 x 4)
        f = 0.5 / jp.tan(fovy * jp.pi / 360.0) * res[1]
        fx, fy = jp.where(
            focal_flag,
            intrinsic[:2] / (sensorsize[:2] + mujoco.mjMINVAL) * res[:2],
            f,
        )  # add mjMINVAL to denominator to prevent divide by zero warning

        focal = jp.array([[-fx, 0, 0, 0], [0, fy, 0, 0], [0, 0, 1.0, 0]])

        # image matrix (3 x 3)
        image = jp.eye(3).at[:2, 2].set(res[0:2] / 2.0)

        # projection matrix (3 x 4): product of all 4 matrices
        proj = image @ focal @ rotation @ translation

        # projection matrix multiplies homogenous [x, y, z, 1] vectors
        pos_hom = jp.append(target_xpos, 1.0)

        # project world coordinates into pixel space, see:
        # https://en.wikipedia.org/wiki/3D_projection#Mathematical_formula
        pixel_coord_hom = proj @ pos_hom

        # avoid dividing by tiny numbers
        denom = pixel_coord_hom[2]
        denom = jp.where(
            jp.abs(denom) < mujoco.mjMINVAL,
            jp.clip(denom, -mujoco.mjMINVAL, mujoco.mjMINVAL),
            denom,
        )

        # compute projection
        sensor = pixel_coord_hom / denom

        return sensor[:2]

      sensorsize = m.cam_sensorsize[refid]
      intrinsic = m.cam_intrinsic[refid]
      fovy = m.cam_fovy[refid]
      res = m.cam_resolution[refid]
      focal_flag = np.logical_and(sensorsize[:, 0] != 0, sensorsize[:, 1] != 0)

      target_xpos = d.site_xpos[objid]
      xpos = d.cam_xpos[refid]
      xmat = d.cam_xmat[refid]

      sensor = _cam_project(
          target_xpos, xpos, xmat, res, fovy, intrinsic, sensorsize, focal_flag
      ).reshape(-1)
      adr = (adr[:, None] + np.arange(2)[None]).reshape(-1)
    elif sensor_type == SensorType.RANGEFINDER:
      site_bodyid = m.site_bodyid[objid]
      for sid in set(site_bodyid):
        idxs = sid == site_bodyid
        objids = objid[idxs]
        site_xpos = d.site_xpos[objids]
        site_mat = d.site_xmat[objids].reshape((-1, 9))[:, np.array([2, 5, 8])]
        sensor, _ = jax.vmap(
            ray.ray, in_axes=(None, None, 0, 0, None, None, None)
        )(m, d, site_xpos, site_mat, (), True, sid)
        sensors.append(sensor)
        adrs.append(adr[idxs])
      continue  # avoid adding to sensors/adrs list a second time
    elif sensor_type == SensorType.JOINTPOS:
      sensor = d.qpos[m.jnt_qposadr[objid]]
    elif sensor_type == SensorType.ACTUATORPOS:
      sensor = d.actuator_length[objid]
    elif sensor_type == SensorType.BALLQUAT:
      jnt_qposadr = m.jnt_qposadr[objid, None] + np.arange(4)[None]
      quat = d.qpos[jnt_qposadr]
      sensor = jax.vmap(math.normalize)(quat).reshape(-1)
      adr = (adr[:, None] + np.arange(4)[None]).reshape(-1)
    elif sensor_type == SensorType.FRAMEPOS:

      def _framepos(xpos, xpos_ref, xmat_ref, refid):
        return jp.where(refid == -1, xpos, xmat_ref.T @ (xpos - xpos_ref))

      # evaluate for valid object and reference object type pairs
      for ot, rt in set(zip(objtype, reftype)):
        idxt = (objtype == ot) & (reftype == rt)
        refidt = refid[idxt]
        xpos, _ = objtype_data[ot]
        xpos_ref, xmat_ref = objtype_data[rt]
        xpos = xpos[objid[idxt]]
        xpos_ref = xpos_ref[refidt]
        xmat_ref = xmat_ref[refidt]
        sensor = jax.vmap(_framepos)(xpos, xpos_ref, xmat_ref, refidt)
        adrt = adr[idxt, None] + np.arange(3)[None]
        sensors.append(sensor.reshape(-1))
        adrs.append(adrt.reshape(-1))
      continue  # avoid adding to sensors/adrs list a second time
    elif sensor_type in frame_axis:

      def _frameaxis(xmat, xmat_ref, refid):
        axis = xmat[:, frame_axis[sensor_type]]
        return jp.where(refid == -1, axis, xmat_ref.T @ axis)

      # evaluate for valid object and reference object type pairs
      for ot, rt in set(zip(objtype, reftype)):
        idxt = (objtype == ot) & (reftype == rt)
        refidt = refid[idxt]
        _, xmat = objtype_data[ot]
        _, xmat_ref = objtype_data[rt]
        xmat = xmat[objid[idxt]]
        xmat_ref = xmat_ref[refidt]
        sensor = jax.vmap(_frameaxis)(xmat, xmat_ref, refidt)
        adrt = adr[idxt, None] + np.arange(3)[None]
        sensors.append(sensor.reshape(-1))
        adrs.append(adrt.reshape(-1))
      continue  # avoid adding to sensors/adrs list a second time
    elif sensor_type == SensorType.FRAMEQUAT:

      def _quat(otype, oid):
        if otype == ObjType.XBODY:
          return d.xquat[oid]
        elif otype == ObjType.BODY:
          return jax.vmap(math.quat_mul)(d.xquat[oid], m.body_iquat[oid])
        elif otype == ObjType.GEOM:
          return jax.vmap(math.quat_mul)(
              d.xquat[m.geom_bodyid[oid]], m.geom_quat[oid]
          )
        elif otype == ObjType.SITE:
          return jax.vmap(math.quat_mul)(
              d.xquat[m.site_bodyid[oid]], m.site_quat[oid]
          )
        elif otype == ObjType.CAMERA:
          return jax.vmap(math.quat_mul)(
              d.xquat[m.cam_bodyid[oid]], m.cam_quat[oid]
          )
        elif otype == ObjType.UNKNOWN:
          return jp.tile(jp.array([1.0, 0.0, 0.0, 0.0]), (oid.size, 1))
        else:
          raise ValueError(f'Unknown object type: {otype}')

      # evaluate for valid object and reference object type pairs
      for ot, rt in set(zip(objtype, reftype)):
        idxt = (objtype == ot) & (reftype == rt)
        objidt = objid[idxt]
        refidt = refid[idxt]
        quat = _quat(ot, objidt)
        refquat = _quat(rt, refidt)
        sensor = jax.vmap(
            lambda q, r, rid: jp.where(
                rid == -1, q, math.quat_mul(math.quat_inv(r), q)
            )
        )(quat, refquat, refidt)
        adrt = adr[idxt, None] + np.arange(4)[None]
        sensors.append(sensor.reshape(-1))
        adrs.append(adrt.reshape(-1))
      continue  # avoid adding to sensors/adrs list a second time
    elif sensor_type == SensorType.SUBTREECOM:
      sensor = d.subtree_com[objid].reshape(-1)
      adr = (adr[:, None] + np.arange(3)[None]).reshape(-1)
    elif sensor_type == SensorType.CLOCK:
      sensor = jp.repeat(d.time, sum(idx))
    else:
      # TODO(taylorhowell): raise error after adding sensor check to io.py
      continue  # unsupported sensor type

    sensors.append(sensor)
    adrs.append(adr)

  if not adrs:
    return d

  sensordata = d.sensordata.at[np.concatenate(adrs)].set(
      jp.concatenate(sensors)
  )

  return d.replace(sensordata=sensordata)


def sensor_vel(m: Model, d: Data) -> Data:
  """Compute velocity-dependent sensors values."""

  if m.opt.disableflags & DisableBit.SENSOR:
    return d

  stage_vel = m.sensor_needstage == mujoco.mjtStage.mjSTAGE_VEL
  sensors, adrs = [], []

  for sensor_type in set(m.sensor_type[stage_vel]):
    idx = m.sensor_type == sensor_type
    objid = m.sensor_objid[idx]
    adr = m.sensor_adr[idx]

    if sensor_type == SensorType.JOINTVEL:
      sensor = d.qvel[m.jnt_dofadr[objid]]
    elif sensor_type == SensorType.ACTUATORVEL:
      sensor = d.actuator_velocity[objid]
    elif sensor_type == SensorType.BALLANGVEL:
      jnt_dotadr = m.jnt_dofadr[objid, None] + np.arange(3)[None]
      sensor = d.qvel[jnt_dotadr].reshape(-1)
      adr = (adr[:, None] + np.arange(3)[None]).reshape(-1)
    else:
      # TODO(taylorhowell): raise error after adding sensor check to io.py
      continue  # unsupported sensor typ

    sensors.append(sensor)
    adrs.append(adr)

  if not adrs:
    return d

  sensordata = d.sensordata.at[np.concatenate(adrs)].set(
      jp.concatenate(sensors)
  )

  return d.replace(sensordata=sensordata)


def sensor_acc(m: Model, d: Data) -> Data:
  """Compute acceleration/force-dependent sensors values."""

  if m.opt.disableflags & DisableBit.SENSOR:
    return d

  stage_acc = m.sensor_needstage == mujoco.mjtStage.mjSTAGE_ACC
  sensors, adrs = [], []

  for sensor_type in set(m.sensor_type[stage_acc]):
    idx = m.sensor_type == sensor_type
    objid = m.sensor_objid[idx]
    adr = m.sensor_adr[idx]

    if sensor_type == SensorType.ACTUATORFRC:
      sensor = d.actuator_force[objid]
    elif sensor_type == SensorType.JOINTACTFRC:
      sensor = d.qfrc_actuator[m.jnt_dofadr[objid]]
    else:
      # TODO(taylorhowell): raise error after adding sensor check to io.py
      continue  # unsupported sensor type

    sensors.append(sensor)
    adrs.append(adr)

  if not adrs:
    return d

  sensordata = d.sensordata.at[np.concatenate(adrs)].set(
      jp.concatenate(sensors)
  )

  return d.replace(sensordata=sensordata)
