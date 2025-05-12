# SPDX-FileCopyrightText: Copyright (c) 2025 The Newton Developers
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import numpy as np
import warp as wp

import newton


def parse_usd(
    source,
    builder: newton.ModelBuilder,
    xform: wp.transform | None = None,
    default_density=1.0e3,
    only_load_enabled_rigid_bodies=False,
    only_load_enabled_joints=True,
    only_load_warp_scene=False,
    contact_ke=1e5,
    contact_kd=250.0,
    contact_kf=500.0,
    contact_ka=0.0,
    contact_mu=0.6,
    contact_restitution=0.0,
    contact_thickness=0.0,
    joint_limit_ke=100.0,
    joint_limit_kd=10.0,
    armature=0.0,
    joint_drive_gains_scaling=1.0,
    invert_rotations=False,
    verbose: bool = wp.config.verbose,
    ignore_paths: list[str] | None = None,
    cloned_env: str | None = None,
    collapse_fixed_joints=False,
    root_path="/",
) -> dict[str, Any]:
    """
    Parses a Universal Scene Description (USD) stage containing UsdPhysics schema definitions for rigid-body articulations and adds the bodies, shapes and joints to the given ModelBuilder.

    The USD description has to be either a path (file name or URL), or an existing USD stage instance that implements the `UsdStage <https://openusd.org/dev/api/class_usd_stage.html>`_ interface.

    Args:
        source (str | pxr.UsdStage): The file path to the USD file, or an existing USD stage instance.
        builder (ModelBuilder): The :class:`ModelBuilder` to add the bodies and joints to.
        xform (wp.transform): The transform to apply to the entire scene.
        default_density (float): The default density to use for bodies without a density attribute.
        only_load_enabled_rigid_bodies (bool): If True, only rigid bodies which do not have `physics:rigidBodyEnabled` set to False are loaded.
        only_load_enabled_joints (bool): If True, only joints which do not have `physics:jointEnabled` set to False are loaded.
        only_load_warp_scene (bool): If True, only load bodies that belong to a PhysicsScene which is simulated by Warp as a simulation owner.
        contact_ke (float): The default contact stiffness to use, if not set on the primitive or the PhysicsScene with as "warp:contact_ke". Only considered by the Euler integrators.
        contact_kd (float): The default contact damping to use, if not set on the primitive or the PhysicsScene with as "warp:contact_kd". Only considered by the Euler integrators.
        contact_kf (float): The default friction stiffness to use, if not set on the primitive or the PhysicsScene with as "warp:contact_kf". Only considered by the Euler integrators.
        contact_ka (float): The default adhesion distance to use, if not set on the primitive or the PhysicsScene with as "warp:contact_ka". Only considered by the Euler integrators.
        contact_mu (float): The default friction coefficient to use if a shape has not friction coefficient defined in a PhysicsMaterial.
        contact_restitution (float): The default coefficient of restitution to use if a shape has not coefficient of restitution defined in a PhysicsMaterial.
        contact_thickness (float): The thickness to add to the shape geometry, if not set on the primitive or the PhysicsScene with as "warp:contact_thickness".
        joint_limit_ke (float): The default stiffness to use for joint limits, if not set on the primitive or the PhysicsScene with as "warp:joint_limit_ke". Only considered by the Euler integrators.
        joint_limit_kd (float): The default damping to use for joint limits, if not set on the primitive or the PhysicsScene with as "warp:joint_limit_kd". Only considered by the Euler integrators.
        armature (float): The default armature to use for the bodies, if not set on the primitive or the PhysicsScene with as "warp:armature".
        joint_drive_gains_scaling (float): The default scaling of the PD control gains (stiffness and damping), if not set in the PhysicsScene with as "warp:joint_drive_gains_scaling".
        invert_rotations (bool): If True, inverts any rotations defined in the shape transforms.
        verbose (bool): If True, print additional information about the parsed USD file.
        ignore_paths (List[str]): A list of regular expressions matching prim paths to ignore.
        cloned_env (str): The prim path of an environment which is cloned within this USD file. Siblings of this environment prim will not be parsed but instead be replicated via `newton.ModelBuilder.add_builder(builder, xform)` to speed up the loading of many instantiated environments.
        collapse_fixed_joints (bool): If True, fixed joints are removed and the respective bodies are merged. Only considered if not set on the PhysicsScene with as "warp:collapse_fixed_joints".
        root_path (str): The USD path to import, defaults to "/".

    Returns:
        dict: Dictionary with the following entries:

        .. list-table::
            :widths: 25 75

            * - "fps"
              - USD stage frames per second
            * - "duration"
              - Difference between end time code and start time code of the USD stage
            * - "up_axis"
              - Upper-case string of the stage's up axis ("X", "Y", or "Z")
            * - "path_shape_map"
              - Mapping from prim path (str) of the UsdGeom to the respective shape index in :class:`ModelBuilder`
            * - "path_body_map"
              - Mapping from prim path (str) of a rigid body prim (e.g. that implements the PhysicsRigidBodyAPI) to the respective body index in :class:`ModelBuilder`
            * - "path_shape_scale"
              - Mapping from prim path (str) of the UsdGeom to its respective 3D world scale
            * - "mass_unit"
              - The stage's Kilograms Per Unit (KGPU) definition (1.0 by default)
            * - "linear_unit"
              - The stage's Meters Per Unit (MPU) definition (1.0 by default)
            * - "scene_attributes"
              - Dictionary of all attributes applied to the PhysicsScene prim
            * - "collapse_results"
              - Dictionary returned by :math:`ModelBuilder.collapse_fixed_joints()` if `collapse_fixed_joints` is True, otherwise None.
    """
    try:
        from pxr import Sdf, Usd, UsdGeom, UsdPhysics
    except ImportError as e:
        raise ImportError("Failed to import pxr. Please install USD (e.g. via `pip install usd-core`).") from e

    from dataclasses import dataclass

    from newton.utils import topological_sort

    @dataclass
    class PhysicsMaterial:
        staticFriction: float = contact_mu
        dynamicFriction: float = contact_mu
        restitution: float = contact_restitution
        density: float = default_density

    incoming_world_xform = xform or wp.transform_identity()

    def get_attribute(prim, name):
        return prim.GetAttribute(name)

    def has_attribute(prim, name):
        attr = get_attribute(prim, name)
        return attr.IsValid() and attr.HasAuthoredValue()

    def parse_float(prim, name, default=None):
        attr = get_attribute(prim, name)
        if not attr or not attr.HasAuthoredValue():
            return default
        val = attr.Get()
        if np.isfinite(val):
            return val
        return default

    def parse_float_with_fallback(prims: Iterable[Usd.Prim], name: str, default=0.0) -> float:
        ret = default
        for prim in prims:
            if not prim:
                continue
            attr = get_attribute(prim, name)
            if not attr or not attr.HasAuthoredValue():
                continue
            val = attr.Get()
            if np.isfinite(val):
                ret = val
                break
        return ret

    def from_gfquat(gfquat):
        return wp.normalize(wp.quat(*gfquat.imaginary, gfquat.real))

    def parse_quat(prim, name, default=None):
        attr = get_attribute(prim, name)
        if not attr or not attr.HasAuthoredValue():
            return default
        val = attr.Get()
        if invert_rotations:
            quat = wp.quat(*val.imaginary, -val.real)
        else:
            quat = wp.quat(*val.imaginary, val.real)
        l = wp.length(quat)
        if np.isfinite(l) and l > 0.0:
            return quat
        return default

    def parse_vec(prim, name, default=None):
        attr = get_attribute(prim, name)
        if not attr or not attr.HasAuthoredValue():
            return default
        val = attr.Get()
        if np.isfinite(val).all():
            return np.array(val, dtype=np.float32)
        return default

    def parse_generic(prim, name, default=None):
        attr = get_attribute(prim, name)
        if not attr or not attr.HasAuthoredValue():
            return default
        return attr.Get()

    def parse_xform(prim):
        xform = UsdGeom.Xform(prim)
        mat = np.array(xform.GetLocalTransformation(), dtype=np.float32)
        if invert_rotations:
            rot = wp.quat_from_matrix(wp.mat33(mat[:3, :3].T.flatten()))
        else:
            rot = wp.quat_from_matrix(wp.mat33(mat[:3, :3].flatten()))
        pos = mat[3, :3]
        return wp.transform(pos, rot)

    if ignore_paths is None:
        ignore_paths = []

    def convert_axis(axis):
        return {
            UsdPhysics.Axis.X: (1.0, 0.0, 0.0),
            UsdPhysics.Axis.Y: (0.0, 1.0, 0.0),
            UsdPhysics.Axis.Z: (0.0, 0.0, 1.0),
        }[axis]

    if isinstance(source, str):
        stage = Usd.Stage.Open(source, Usd.Stage.LoadAll)
    else:
        stage = source

    def get_up_vector_and_axis(stage):
        # First entry on tuple is vector and second entry on tuple is axis
        _vector_and_axis = {
            "X": (wp.vec3(1.0, 0.0, 0.0), 0),
            "Y": (wp.vec3(0.0, 1.0, 0.0), 1),
            "Z": (wp.vec3(0.0, 0.0, 1.0), 2),
        }
        up_symbol = UsdGeom.GetStageUpAxis(stage)
        return _vector_and_axis[up_symbol]

    DegreesToRadian = np.pi / 180
    mass_unit = 1.0

    try:
        if UsdPhysics.StageHasAuthoredKilogramsPerUnit(stage):
            mass_unit = UsdPhysics.GetStageKilogramsPerUnit(stage)
    except Exception as e:
        if verbose:
            print(f"Failed to get mass unit: {e}")
    linear_unit = 1.0
    try:
        if UsdGeom.StageHasAuthoredMetersPerUnit(stage):
            linear_unit = UsdGeom.GetStageMetersPerUnit(stage)
    except Exception as e:
        if verbose:
            print(f"Failed to get linear unit: {e}")

    # resolve cloned environments
    if cloned_env is not None:
        cloned_env_prim = stage.GetPrimAtPath(cloned_env)
        if not cloned_env_prim:
            raise RuntimeError(f"Failed to resolve cloned environment {cloned_env}")
        cloned_env_xforms = []
        cloned_env_paths = []
        # get paths of the siblings of the cloned env
        # and ignore them during parsing, later we use
        # ModelBuilder.add_builder() to instantiate these
        # envs at their respective Xform transforms
        envs_prim = cloned_env_prim.GetParent()
        for sibling in envs_prim.GetChildren():
            # print(sibling.GetPath(), parse_xform(sibling))
            p = str(sibling.GetPath())
            cloned_env_xforms.append(parse_xform(sibling))
            cloned_env_paths.append(p)
            if sibling != cloned_env_prim:
                ignore_paths.append(p)

        # set xform of the cloned env (e.g. "env0") to identity
        # and later apply this xform via ModelBuilder.add_builder()
        # to instantiate the env at the correct location
        UsdGeom.Xform(cloned_env_prim).SetXformOpOrder([])

        # create a new builder for the cloned env, then instantiate
        # it back in the original builder
        multi_env_builder = builder
        builder = newton.ModelBuilder()

    # ret_dict = PhysicsUtils.LoadUsdPhysicsFromRange(
    #     stage, PhysicsUtils.ParsePrimIteratorRange(Usd.PrimRange(stage.GetPseudoRoot()))
    # )
    ret_dict = UsdPhysics.LoadUsdPhysicsFromRange(stage, [root_path], excludePaths=ignore_paths)
    # print("********************** LoadUsdPhysicsFromRange")

    # for key, value in ret_dict.items():
    #     print(f"Object type: {key}")
    #     prims, scene_descs = value
    #     for prim, desc in zip(prims, scene_descs):
    #         print(prim)
    #         print(desc)
    #     if key == UsdPhysics.ObjectType.CapsuleShape:
    #         for prim, desc in zip(prims, scene_descs):
    #             print(desc.halfHeight)
    #     if key == UsdPhysics.ObjectType.RigidBody:
    #         for prim, desc in zip(prims, scene_descs):
    #             print(desc.simulationOwners)
    # print("***************************************************************************")

    # mapping from prim path to body ID in Warp sim
    path_body_map = {}
    # mapping from prim path to shape ID in Warp sim
    path_shape_map = {}
    path_shape_scale = {}

    physics_scene_prim = None

    def is_warp_scene(prim):
        if "warpSceneAPI" in prim.GetPrimTypeInfo().GetAppliedAPISchemas():
            # print(prim.GetPath(), "is a warp scene")
            return True
        else:
            # print(prim.GetPath(), "is NOT a warp scene")
            return False

    def is_warp_body(prim):
        if not only_load_warp_scene:
            return True
        if prim.HasAPI(UsdPhysics.RigidBodyAPI):
            rigidBodyAPI = UsdPhysics.RigidBodyAPI.Get(stage, prim.GetPath())
            targets = rigidBodyAPI.GetSimulationOwnerRel().GetTargets()
            if len(targets) > 0:
                physics_scene = stage.GetPrimAtPath(targets[0])
                # print("rigid body %s scene = %s" % (prim.GetPath(), targets[0]))
                return is_warp_scene(physics_scene)
        return False

    def parse_body(rigid_body_desc, prim, articulation_root=False, incoming_xform=None):
        nonlocal path_body_map
        nonlocal physics_scene_prim

        use_warp = is_warp_body(prim)
        # print("is_warp_body ", prim, " ", use_warp)
        if use_warp:
            prim.CreateAttribute("physics:engine", Sdf.ValueTypeNames.String).Set("warp")
        else:
            return -1

        if not rigid_body_desc.rigidBodyEnabled and only_load_enabled_rigid_bodies:
            return -1
        rot = rigid_body_desc.rotation
        origin = wp.transform(rigid_body_desc.position, from_gfquat(rot))
        if incoming_xform is not None:
            origin = wp.mul(incoming_xform, origin)
        path = str(prim.GetPath())

        body_armature = parse_float_with_fallback((prim, physics_scene_prim), "warp:armature", armature)

        b = builder.add_body(
            origin=origin,
            key=path,
            armature=body_armature,
        )
        path_body_map[path] = b
        return b

    def parse_scale(prim):
        xform = UsdGeom.Xform(prim)
        scale = np.ones(3, dtype=np.float32)
        for op in xform.GetOrderedXformOps():
            if op.GetOpType() == UsdGeom.XformOp.TypeScale:
                scale = np.array(op.Get(), dtype=np.float32)
        return scale

    def parse_joint(joint_desc, joint_path, incoming_xform=None):
        if not joint_desc.jointEnabled and only_load_enabled_joints:
            return
        key = joint_desc.type
        joint_prim = stage.GetPrimAtPath(joint_desc.primPath)
        parent_path = str(joint_desc.body0)
        child_path = str(joint_desc.body1)
        parent_id = path_body_map.get(parent_path, -1)
        # if parent_id == -1:
        #     print("joint connected to world")
        child_id = path_body_map.get(child_path, -1)
        parent_tf = wp.transform(joint_desc.localPose0Position, from_gfquat(joint_desc.localPose0Orientation))
        if incoming_xform is not None:
            parent_tf = wp.mul(incoming_xform, parent_tf)
        child_tf = wp.transform(joint_desc.localPose1Position, from_gfquat(joint_desc.localPose1Orientation))

        joint_armature = armature
        if has_attribute(joint_prim, "physxJoint:armature"):
            joint_armature = parse_float(joint_prim, "physxJoint:armature")
        joint_params = {
            "parent": parent_id,
            "child": child_id,
            "parent_xform": parent_tf,
            "child_xform": child_tf,
            "key": str(joint_path),
            "enabled": joint_desc.jointEnabled,
            "armature": joint_armature,
        }
        current_joint_limit_ke = parse_float_with_fallback(
            (joint_prim, physics_scene_prim), "warp:joint_limit_ke", joint_limit_ke
        )
        current_joint_limit_kd = parse_float_with_fallback(
            (joint_prim, physics_scene_prim), "warp:joint_limit_kd", joint_limit_kd
        )

        if key == UsdPhysics.ObjectType.FixedJoint:
            builder.add_joint_fixed(**joint_params)
        elif key == UsdPhysics.ObjectType.RevoluteJoint or key == UsdPhysics.ObjectType.PrismaticJoint:
            joint_params["axis"] = convert_axis(joint_desc.axis)
            joint_params["limit_lower"] = joint_desc.limit.lower
            joint_params["limit_upper"] = joint_desc.limit.upper
            joint_params["limit_ke"] = current_joint_limit_ke
            joint_params["limit_kd"] = current_joint_limit_kd
            if joint_desc.drive.enabled:
                # XXX take the target which is nonzero to decide between position vs. velocity target...
                if joint_desc.drive.targetVelocity:
                    joint_params["target"] = joint_desc.drive.targetVelocity
                    joint_params["mode"] = wp.sim.JOINT_MODE_TARGET_VELOCITY
                else:
                    joint_params["target"] = joint_desc.drive.targetPosition
                    joint_params["mode"] = wp.sim.JOINT_MODE_TARGET_POSITION

                joint_params["target_ke"] = joint_desc.drive.stiffness * joint_drive_gains_scaling
                joint_params["target_kd"] = joint_desc.drive.damping * joint_drive_gains_scaling

            if key == UsdPhysics.ObjectType.PrismaticJoint:
                joint_prim.CreateAttribute("physics:tensor:linear:dofOffset", Sdf.ValueTypeNames.UInt).Set(0)
                joint_prim.CreateAttribute("state:angular:physics:position", Sdf.ValueTypeNames.Float).Set(0)
                joint_prim.CreateAttribute("state:angular:physics:velocity", Sdf.ValueTypeNames.Float).Set(0)
                builder.add_joint_prismatic(**joint_params)
            else:
                joint_prim.CreateAttribute("physics:tensor:angular:dofOffset", Sdf.ValueTypeNames.UInt).Set(0)
                joint_prim.CreateAttribute("state:angular:physics:position", Sdf.ValueTypeNames.Float).Set(0)
                joint_prim.CreateAttribute("state:angular:physics:velocity", Sdf.ValueTypeNames.Float).Set(0)

                if joint_desc.drive.enabled:
                    joint_params["target"] *= DegreesToRadian
                    joint_params["target_kd"] /= DegreesToRadian / joint_drive_gains_scaling
                    joint_params["target_ke"] /= DegreesToRadian / joint_drive_gains_scaling

                joint_params["limit_lower"] *= DegreesToRadian
                joint_params["limit_upper"] *= DegreesToRadian
                joint_params["limit_ke"] /= DegreesToRadian / joint_drive_gains_scaling
                joint_params["limit_kd"] /= DegreesToRadian / joint_drive_gains_scaling

                builder.add_joint_revolute(**joint_params)
        elif key == UsdPhysics.ObjectType.SphericalJoint:
            builder.add_joint_ball(**joint_params)
        elif key == UsdPhysics.ObjectType.D6Joint:
            linear_axes = []
            angular_axes = []
            num_dofs = 0
            # print(joint_desc.jointLimits, joint_desc.jointDrives)
            # print(joint_desc.body0)
            # print(joint_desc.body1)
            # print(joint_desc.jointLimits)
            # print("Limits")
            # for limit in joint_desc.jointLimits:
            #     print("joint_path :", joint_path, limit.first, limit.second.lower, limit.second.upper)
            # print("Drives")
            # for drive in joint_desc.jointDrives:
            #     print("joint_path :", joint_path, drive.first, drive.second.targetPosition, drive.second.targetVelocity)

            for limit in joint_desc.jointLimits:
                dof = limit.first
                if limit.second.enabled:
                    limit_lower = limit.second.lower
                    limit_upper = limit.second.upper
                else:
                    limit_lower = -np.inf
                    limit_upper = np.inf

                free_axis = limit_lower < limit_upper

                def define_joint_mode(dof, joint_desc):
                    action = 0.0  # TODO: parse action from state:*:physics:appliedForce usd attribute when no drive is present
                    mode = wp.sim.JOINT_MODE_FORCE
                    target_ke = 0.0
                    target_kd = 0.0
                    for drive in joint_desc.jointDrives:
                        if drive.first != dof:
                            continue
                        if drive.second.enabled:
                            if drive.second.targetVelocity != 0.0:
                                action = drive.second.targetVelocity
                                mode = wp.sim.JOINT_MODE_VELOCITY
                            else:
                                action = drive.second.targetPosition
                                mode = wp.sim.JOINT_MODE_TARGET_POSITION
                            target_ke = drive.second.stiffness
                            target_kd = drive.second.damping
                    return action, mode, target_ke, target_kd

                action, mode, target_ke, target_kd = define_joint_mode(dof, joint_desc)

                _trans_axes = {
                    UsdPhysics.JointDOF.TransX: (1.0, 0.0, 0.0),
                    UsdPhysics.JointDOF.TransY: (0.0, 1.0, 0.0),
                    UsdPhysics.JointDOF.TransZ: (0.0, 0.0, 1.0),
                }
                _rot_axes = {
                    UsdPhysics.JointDOF.RotX: (1.0, 0.0, 0.0),
                    UsdPhysics.JointDOF.RotY: (0.0, 1.0, 0.0),
                    UsdPhysics.JointDOF.RotZ: (0.0, 0.0, 1.0),
                }
                _rot_names = {
                    UsdPhysics.JointDOF.RotX: "rotX",
                    UsdPhysics.JointDOF.RotY: "rotY",
                    UsdPhysics.JointDOF.RotZ: "rotZ",
                }
                if free_axis and dof in _trans_axes:
                    linear_axes.append(
                        wp.sim.JointAxis(
                            axis=_trans_axes[dof],
                            limit_lower=limit_lower,
                            limit_upper=limit_upper,
                            limit_ke=current_joint_limit_ke,
                            limit_kd=current_joint_limit_kd,
                            action=action,
                            mode=mode,
                            target_ke=target_ke,
                            target_kd=target_kd,
                        )
                    )
                elif free_axis and dof in _rot_axes:
                    angular_axes.append(
                        wp.sim.JointAxis(
                            axis=_rot_axes[dof],
                            limit_lower=limit_lower * DegreesToRadian,
                            limit_upper=limit_upper * DegreesToRadian,
                            limit_ke=current_joint_limit_ke / DegreesToRadian / joint_drive_gains_scaling,
                            limit_kd=current_joint_limit_kd / DegreesToRadian / joint_drive_gains_scaling,
                            action=action * DegreesToRadian,
                            mode=mode,
                            target_ke=target_ke / DegreesToRadian / joint_drive_gains_scaling,
                            target_kd=target_kd / DegreesToRadian / joint_drive_gains_scaling,
                        )
                    )
                    joint_prim.CreateAttribute(
                        f"physics:tensor:{_rot_names[dof]}:dofOffset", Sdf.ValueTypeNames.UInt
                    ).Set(num_dofs)
                    joint_prim.CreateAttribute(
                        f"state:{_rot_names[dof]}:physics:position", Sdf.ValueTypeNames.Float
                    ).Set(0)
                    joint_prim.CreateAttribute(
                        f"state:{_rot_names[dof]}:physics:velocity", Sdf.ValueTypeNames.Float
                    ).Set(0)
                    num_dofs += 1

            builder.add_joint_d6(**joint_params, linear_axes=linear_axes, angular_axes=angular_axes)
        elif key == UsdPhysics.ObjectType.DistanceJoint:
            if joint_desc.limit.enabled and joint_desc.minEnabled:
                min_dist = joint_desc.limit.lower
            else:
                min_dist = -1.0  # no limit
            if joint_desc.limit.enabled and joint_desc.maxEnabled:
                max_dist = joint_desc.limit.upper
            else:
                max_dist = -1.0
            builder.add_joint_distance(**joint_params, min_distance=min_dist, max_distance=max_dist)
        else:
            raise NotImplementedError(f"Unsupported joint type {key}")

    # Looking for and parsing the attributes on PhysicsScene prims
    scene_attributes = {}
    if UsdPhysics.ObjectType.Scene in ret_dict:
        paths, scene_descs = ret_dict[UsdPhysics.ObjectType.Scene]
        if len(paths) > 1 and verbose:
            print("Only the first PhysicsScene is considered")
        path, scene_desc = paths[0], scene_descs[0]
        if verbose:
            print("Found PhysicsScene:", path)
            print("Gravity direction:", scene_desc.gravityDirection)
            print("Gravity magnitude:", scene_desc.gravityMagnitude)
        builder.gravity = -scene_desc.gravityMagnitude * linear_unit
        builder.up_vector = -np.array(scene_desc.gravityDirection, dtype=np.float32)
        builder.up_axis = np.argmax(np.abs(builder.up_vector))

        # Storing Physics Scene attributes
        physics_scene_prim = stage.GetPrimAtPath(path)
        for a in physics_scene_prim.GetAttributes():
            scene_attributes[a.GetName()] = a.Get()

        # Updating joint_drive_gains_scaling if set of the PhysicsScene
        joint_drive_gains_scaling = parse_float(
            physics_scene_prim, "warp:joint_drive_gains_scaling", joint_drive_gains_scaling
        )
    else:
        builder.up_vector, builder.up_axis = get_up_vector_and_axis(stage)

    if verbose:
        print(
            f"Scaling PD gains by (joint_drive_gains_scaling / DegreesToRadian) = {joint_drive_gains_scaling / DegreesToRadian}, default scale for joint_drive_gains_scaling=1 is 1.0/DegreesToRadian = {1.0 / DegreesToRadian}"
        )

    joint_descriptions = {}
    body_specs = {}
    material_specs = {}
    # maps from rigid body path to density value if it has been defined
    body_density = {}
    # maps from articulation_id to list of body_ids
    articulation_bodies = {}
    articulation_roots = []

    # TODO: uniform interface for iterating
    def data_for_key(physics_utils_results, key):
        if key not in physics_utils_results:
            return
        if verbose:
            print(physics_utils_results[key])

        yield from zip(*physics_utils_results[key])

    # Setting up the default material
    material_specs[""] = PhysicsMaterial()

    # Parsing physics materials from the stage
    for sdf_path, desc in data_for_key(ret_dict, UsdPhysics.ObjectType.RigidBodyMaterial):
        material_specs[str(sdf_path)] = PhysicsMaterial(
            staticFriction=desc.staticFriction,
            dynamicFriction=desc.dynamicFriction,
            restitution=desc.restitution,
            # TODO: if desc.density is 0, then we should look for mass somewhere
            density=desc.density if desc.density > 0.0 else default_density,
        )

    if UsdPhysics.ObjectType.RigidBody in ret_dict:
        prim_paths, rigid_body_descs = ret_dict[UsdPhysics.ObjectType.RigidBody]
        for prim_path, rigid_body_desc in zip(prim_paths, rigid_body_descs):
            body_path = str(prim_path)
            body_specs[body_path] = rigid_body_desc
            body_density[body_path] = default_density
            prim = stage.GetPrimAtPath(prim_path)
            # Marking for deprecation --->
            if prim.HasRelationship("material:binding:physics"):
                other_paths = prim.GetRelationship("material:binding:physics").GetTargets()
                if len(other_paths) > 0:
                    material = material_specs[str(other_paths[0])]
                    if material.density > 0.0:
                        body_density[body_path] = material.density

            if "PhysicsMassAPI" in prim.GetAppliedSchemas():
                if has_attribute(prim, "physics:density"):
                    d = parse_float(prim, "physics:density")
                    density = d * mass_unit  # / (linear_unit**3)
                    body_density[body_path] = density
            # <--- Marking for deprecation

    if UsdPhysics.ObjectType.Articulation in ret_dict:
        for key, value in ret_dict.items():
            if key in {
                UsdPhysics.ObjectType.FixedJoint,
                UsdPhysics.ObjectType.RevoluteJoint,
                UsdPhysics.ObjectType.PrismaticJoint,
                UsdPhysics.ObjectType.SphericalJoint,
                UsdPhysics.ObjectType.D6Joint,
                UsdPhysics.ObjectType.DistanceJoint,
            }:
                paths, joint_specs = value
                for path, joint_spec in zip(paths, joint_specs):
                    joint_descriptions[str(path)] = joint_spec

        paths, articulation_descs = ret_dict[UsdPhysics.ObjectType.Articulation]
        # maps from articulation_id to bool indicating if self-collisions are enabled
        articulation_has_self_collision = {}

        articulation_id = builder.articulation_count
        for path, desc in zip(paths, articulation_descs):
            prim = stage.GetPrimAtPath(path)
            builder.add_articulation(str(path))
            body_ids = {}
            current_body_id = 0
            art_bodies = []
            # print("Articulated bodies for ", str(prim.GetPath()))
            for p in desc.articulatedBodies:
                # print(p)
                key = str(p)

                if p == Sdf.Path.emptyPath:
                    current_body_id = -1
                else:
                    usd_prim = stage.GetPrimAtPath(Sdf.Path(key))
                    if "TensorPhysicsArticulationRootAPI" in usd_prim.GetPrimTypeInfo().GetAppliedAPISchemas():
                        usd_prim.CreateAttribute("physics:warp:articulationIndex", Sdf.ValueTypeNames.UInt, True).Set(
                            articulation_id
                        )
                        articulation_roots.append(key)

                body_ids[key] = current_body_id
                current_body_id += 1
                if key in body_specs:
                    # look up description and add body to builder
                    root_body = True if key in articulation_roots else False
                    body_id = parse_body(
                        body_specs[key],
                        stage.GetPrimAtPath(p),
                        articulation_root=root_body,
                    )
                    if body_id >= 0:
                        art_bodies.append(body_id)
                    # remove body spec once we inserted it
                    del body_specs[key]

            joint_names = []
            joint_edges: list[tuple[int, int]] = []
            for p in desc.articulatedJoints:
                joint_names.append(str(p))
                joint_desc = joint_descriptions[str(p)]
                joint_edges.append((body_ids[str(joint_desc.body0)], body_ids[str(joint_desc.body1)]))

            # add joints in topological order
            sorted_joints = topological_sort(joint_edges)
            # sorted_joints = np.arange(len(joint_names))
            articulation_xform = wp.mul(incoming_world_xform, parse_xform(prim))
            first_joint_parent = joint_edges[sorted_joints[0]][0]
            if first_joint_parent != -1:
                # the mechanism is floating since there is no joint connecting it to the world
                # we explicitly add a free joint to make sure Featherstone can simulate it
                builder.add_joint_free(child=art_bodies[first_joint_parent])
                builder.joint_q[-7:] = articulation_xform
            for joint_id, i in enumerate(sorted_joints):
                if joint_id == 0 and first_joint_parent == -1:
                    # the articulation root joint receives the articulation transform as parent transform
                    # except if we already inserted a floating-base joint
                    parse_joint(
                        joint_descriptions[joint_names[i]],
                        joint_path=joint_names[i],
                        incoming_xform=articulation_xform,
                    )
                else:
                    parse_joint(
                        joint_descriptions[joint_names[i]],
                        joint_path=joint_names[i],
                    )

            articulation_bodies[articulation_id] = art_bodies
            # determine if self-collisions are enabled
            articulation_has_self_collision[articulation_id] = parse_generic(
                prim,
                "physxArticulation:enabledSelfCollisions",
                default=True,
            )
            articulation_id += 1

    # insert remaining bodies that were not part of any articulation so far
    for path, rigid_body_desc in body_specs.items():
        parse_body(
            rigid_body_desc,
            stage.GetPrimAtPath(path),
            incoming_xform=incoming_world_xform,
        )

    # parse shapes attached to the rigid bodies
    path_collision_filters = set()
    no_collision_shapes = set()
    collision_group_ids = {}
    for key, value in ret_dict.items():
        if key in {
            UsdPhysics.ObjectType.CubeShape,
            UsdPhysics.ObjectType.SphereShape,
            UsdPhysics.ObjectType.CapsuleShape,
            UsdPhysics.ObjectType.CylinderShape,
            UsdPhysics.ObjectType.ConeShape,
            UsdPhysics.ObjectType.MeshShape,
            UsdPhysics.ObjectType.PlaneShape,
        }:
            paths, shape_specs = value
            for xpath, shape_spec in zip(paths, shape_specs):
                prim = stage.GetPrimAtPath(xpath)
                # print(prim)
                # print(shape_spec)
                path = str(xpath)
                body_path = str(shape_spec.rigidBody)
                # print("shape ", prim, "body =" , body_path)
                body_id = path_body_map.get(body_path, -1)
                # scale = np.array(shape_spec.localScale)
                scale = parse_scale(prim)
                collision_group = -1
                if len(shape_spec.collisionGroups) > 0:
                    cgroup_name = str(shape_spec.collisionGroups[0])
                    if cgroup_name not in collision_group_ids:
                        collision_group_ids[cgroup_name] = len(collision_group_ids)
                    collision_group = collision_group_ids[cgroup_name]
                material = material_specs[""]
                if len(shape_spec.materials) >= 1:
                    if len(shape_spec.materials) > 1 and verbose:
                        print(f"Warning: More than one material found on shape at '{path}'.\nUsing only the first one.")
                    material = material_specs[str(shape_spec.materials[0])]
                    if verbose:
                        print(
                            f"\tMaterial of '{path}':\tfriction: {material.dynamicFriction},\trestitution: {material.restitution},\tdensity: {material.density}"
                        )
                elif verbose:
                    print(f"No material found for shape at '{path}'.")
                prim_and_scene = (prim, physics_scene_prim)
                shape_params = {
                    "body": body_id,
                    "pos": shape_spec.localPos,
                    "rot": from_gfquat(shape_spec.localRot),
                    "ke": parse_float_with_fallback(prim_and_scene, "warp:contact_ke", contact_ke),
                    "kd": parse_float_with_fallback(prim_and_scene, "warp:contact_kd", contact_kd),
                    "kf": parse_float_with_fallback(prim_and_scene, "warp:contact_kf", contact_kf),
                    "ka": parse_float_with_fallback(prim_and_scene, "warp:contact_ka", contact_ka),
                    "thickness": parse_float_with_fallback(prim_and_scene, "warp:contact_thickness", contact_thickness),
                    "mu": material.dynamicFriction,
                    "restitution": material.restitution,
                    "density": material.density,
                    "collision_group": collision_group,
                    "key": path,
                }
                # print(path, shape_params)
                if key == UsdPhysics.ObjectType.CubeShape:
                    hx, hy, hz = shape_spec.halfExtents
                    shape_id = builder.add_shape_box(
                        **shape_params,
                        hx=hx,
                        hy=hy,
                        hz=hz,
                    )
                elif key == UsdPhysics.ObjectType.SphereShape:
                    shape_id = builder.add_shape_sphere(
                        **shape_params,
                        radius=shape_spec.radius * scale[0],
                    )
                elif key == UsdPhysics.ObjectType.CapsuleShape:
                    shape_id = builder.add_shape_capsule(
                        **shape_params,
                        radius=shape_spec.radius * scale[(int(shape_spec.axis) + 1) % 3],
                        half_height=shape_spec.halfHeight * scale[int(shape_spec.axis)],
                        up_axis=int(shape_spec.axis),
                    )
                elif key == UsdPhysics.ObjectType.CylinderShape:
                    # shape_id = builder.add_shape_cylinder(
                    #     **shape_params,
                    #     radius=shape_spec.radius * scale[(int(shape_spec.axis) + 1) % 3],
                    #     half_height=shape_spec.halfHeight * scale[int(shape_spec.axis)],
                    #     up_axis=int(shape_spec.axis),
                    # )
                    shape_id = builder.add_shape_capsule(
                        **shape_params,
                        radius=shape_spec.radius * scale[(int(shape_spec.axis) + 1) % 3],
                        half_height=shape_spec.halfHeight * scale[int(shape_spec.axis)],
                        up_axis=int(shape_spec.axis),
                    )
                elif key == UsdPhysics.ObjectType.ConeShape:
                    shape_id = builder.add_shape_cone(
                        **shape_params,
                        radius=shape_spec.radius * scale[(int(shape_spec.axis) + 1) % 3],
                        half_height=shape_spec.halfHeight * scale[int(shape_spec.axis)],
                        up_axis=int(shape_spec.axis),
                    )
                elif key == UsdPhysics.ObjectType.MeshShape:
                    mesh = UsdGeom.Mesh(prim)
                    points = np.array(mesh.GetPointsAttr().Get(), dtype=np.float32)
                    indices = np.array(mesh.GetFaceVertexIndicesAttr().Get(), dtype=np.float32)
                    counts = mesh.GetFaceVertexCountsAttr().Get()
                    faces = []
                    face_id = 0
                    for count in counts:
                        if count == 3:
                            faces.append(indices[face_id : face_id + 3])
                        elif count == 4:
                            faces.append(indices[face_id : face_id + 3])
                            faces.append(indices[[face_id, face_id + 2, face_id + 3]])
                        elif verbose:
                            print(
                                f"Error while parsing USD mesh {path}: encountered polygon with {count} vertices, but only triangles and quads are supported."
                            )
                            continue
                        face_id += count
                    m = wp.sim.Mesh(points, np.array(faces, dtype=np.int32).flatten())
                    shape_id = builder.add_shape_mesh(
                        scale=scale,
                        mesh=m,
                        **shape_params,
                    )
                elif key == UsdPhysics.ObjectType.PlaneShape:
                    plane_eq = [0.0, 1.0, 0.0, 0.0]  # Warp uses +Y convention for planes
                    if shape_spec.axis != UsdPhysics.Axis.Y:
                        plane_normal = wp.vec3(0.0, 0.0, 0.0)
                        plane_normal[int(shape_spec.axis)] = 1.0
                        plane_normal = wp.quat_rotate(shape_params["rot"], plane_normal)
                        plane_eq[:3] = plane_normal
                        plane_eq[3] = wp.dot(plane_normal, shape_params["pos"])

                        # rot needs to be recomputed relative to a plane with +Y normal
                        del shape_params["rot"]
                    del shape_params["density"]
                    shape_id = builder.add_shape_plane(
                        **shape_params,
                        plane=plane_eq,
                    )
                else:
                    raise NotImplementedError(f"Shape type {key} not supported yet")
                path_shape_map[path] = shape_id
                path_shape_scale[path] = scale

                schemas = set(prim.GetAppliedSchemas())
                if prim.HasRelationship("physics:filteredPairs"):
                    other_paths = prim.GetRelationship("physics:filteredPairs").GetTargets()
                    for other_path in other_paths:
                        path_collision_filters.add((path, str(other_path)))

                if "PhysicsCollisionAPI" not in schemas or not parse_generic(prim, "physics:collisionEnabled", True):
                    # print("no_collision_shapes : ", prim)
                    no_collision_shapes.add(shape_id)
            # print(path_shape_map)

    # apply collision filters now that we have added all shapes
    for path1, path2 in path_collision_filters:
        shape1 = path_shape_map[path1]
        shape2 = path_shape_map[path2]
        builder.shape_collision_filter_pairs.add((shape1, shape2))

    # apply collision filters to all shapes that have no collision
    for shape_id in no_collision_shapes:
        for other_shape_id in range(builder.shape_count):
            if other_shape_id != shape_id:
                builder.shape_collision_filter_pairs.add((shape_id, other_shape_id))

    # apply collision filters from articulations that have self collisions disabled
    for art_id, bodies in articulation_bodies.items():
        if not articulation_has_self_collision[art_id]:
            for body1 in bodies:
                for body2 in bodies:
                    if body1 != body2:
                        for shape1 in builder.body_shapes[body1]:
                            for shape2 in builder.body_shapes[body2]:
                                builder.shape_collision_filter_pairs.add((shape1, shape2))

    # overwrite inertial properties of bodies that have PhysicsMassAPI schema applied
    if UsdPhysics.ObjectType.RigidBody in ret_dict:
        paths, rigid_body_descs = ret_dict[UsdPhysics.ObjectType.RigidBody]
        for path, _rigid_body_desc in zip(paths, rigid_body_descs):
            prim = stage.GetPrimAtPath(path)
            if "PhysicsMassAPI" not in prim.GetAppliedSchemas():
                continue
            body_path = str(path)
            body_id = path_body_map.get(body_path, -1)
            mass = parse_float(prim, "physics:mass")
            if mass is not None:
                builder.body_mass[body_id] = mass
                builder.body_inv_mass[body_id] = 1.0 / mass
            com = parse_vec(prim, "physics:centerOfMass")
            if com is not None:
                builder.body_com[body_id] = com
            i_diag = parse_vec(prim, "physics:diagonalInertia", np.zeros(3, dtype=np.float32))
            i_rot = parse_quat(prim, "physics:principalAxes", wp.quat_identity())
            if np.linalg.norm(i_diag) > 0.0:
                rot = np.array(wp.quat_to_matrix(i_rot), dtype=np.float32).reshape(3, 3)
                inertia = rot @ np.diag(i_diag) @ rot.T
                builder.body_inertia[body_id] = inertia
                if inertia.any():
                    builder.body_inv_inertia[body_id] = wp.inverse(wp.mat33(*inertia))
                else:
                    builder.body_inv_inertia[body_id] = wp.mat33(*np.zeros((3, 3), dtype=np.float32))

    collapse_results = None
    merged_body_data = {}
    path_body_relative_transform = {}
    if scene_attributes.get("warp:collapse_fixed_joints", collapse_fixed_joints):
        collapse_results = builder.collapse_fixed_joints()
        body_merged_parent = collapse_results["body_merged_parent"]
        body_merged_transform = collapse_results["body_merged_transform"]
        body_remap = collapse_results["body_remap"]
        # remap body ids in articulation bodies
        for art_id, bodies in articulation_bodies.items():
            articulation_bodies[art_id] = [body_remap[b] for b in bodies if b in body_remap]

        for path, body_id in path_body_map.items():
            if body_id in body_remap:
                new_id = body_remap[body_id]
            else:
                new_id = body_remap[body_merged_parent[body_id]]
                path_body_relative_transform[path] = body_merged_transform[body_id]

            path_body_map[path] = new_id
        merged_body_data = collapse_results["merged_body_data"]

    path_original_body_map = path_body_map.copy()
    if cloned_env is not None:
        with wp.ScopedTimer("replicating envs"):
            # instantiate environments
            path_shape_map_updates = {}
            path_body_map_updates = {}
            path_shape_scale_updates = {}
            articulation_roots_updates = []
            articulation_bodies_updates = {}
            articulation_key = builder.articulation_key
            shape_key = builder.shape_key
            joint_key = builder.joint_key
            body_key = builder.body_key
            for env_path, env_xform in zip(cloned_env_paths, cloned_env_xforms):
                shape_count = multi_env_builder.shape_count
                body_count = multi_env_builder.body_count
                original_body_count = multi_env_builder.body_count
                art_count = multi_env_builder.articulation_count
                # print("articulation_bodies = ", articulation_bodies)
                for path, shape_id in path_shape_map.items():
                    new_path = path.replace(cloned_env, env_path)
                    path_shape_map_updates[new_path] = shape_id + shape_count
                for path, body_id in path_body_map.items():
                    new_path = path.replace(cloned_env, env_path)
                    path_body_map_updates[new_path] = body_id + body_count
                    if collapse_fixed_joints:
                        original_body_id = path_original_body_map[path]
                        path_original_body_map[new_path] = original_body_id + original_body_count
                    if path in merged_body_data:
                        merged_body_data[new_path] = merged_body_data[path]
                        parent_path = merged_body_data[path]["parent_body"]
                        new_parent_path = parent_path.replace(cloned_env, env_path)
                        merged_body_data[new_path]["parent_body"] = new_parent_path

                for path, scale in path_shape_scale.items():
                    new_path = path.replace(cloned_env, env_path)
                    path_shape_scale_updates[new_path] = scale
                for art_id, bodies in articulation_bodies.items():
                    articulation_bodies_updates[art_id + art_count] = [b + body_count for b in bodies]
                for root in articulation_roots:
                    new_path = root.replace(cloned_env, env_path)
                    articulation_roots_updates.append(new_path)

                builder.articulation_key = [key.replace(cloned_env, env_path) for key in articulation_key]
                builder.shape_key = [key.replace(cloned_env, env_path) for key in shape_key]
                builder.joint_key = [key.replace(cloned_env, env_path) for key in joint_key]
                builder.body_key = [key.replace(cloned_env, env_path) for key in body_key]
                multi_env_builder.add_builder(builder, xform=env_xform)

            path_shape_map = path_shape_map_updates
            path_body_map = path_body_map_updates
            path_shape_scale = path_shape_scale_updates
            articulation_roots = articulation_roots_updates
            articulation_bodies = articulation_bodies_updates

            builder = multi_env_builder

    return {
        "fps": stage.GetFramesPerSecond(),
        "duration": stage.GetEndTimeCode() - stage.GetStartTimeCode(),
        "up_axis": UsdGeom.GetStageUpAxis(stage).upper(),
        "path_shape_map": path_shape_map,
        "path_body_map": path_body_map,
        "path_shape_scale": path_shape_scale,
        "mass_unit": mass_unit,
        "linear_unit": linear_unit,
        "scene_attributes": scene_attributes,
        "collapse_results": collapse_results,
        # "articulation_roots": articulation_roots,
        # "articulation_bodies": articulation_bodies,
        "path_body_relative_transform": path_body_relative_transform,
    }


def resolve_usd_from_url(url: str, target_folder_name: str | None = None, export_usda: bool = False) -> str:
    """Download a USD file from a URL and resolves all references to other USD files to be downloaded to the given target folder.

    Args:
        url: URL to the USD file.
        target_folder_name: Target folder name. If ``None``, a time-stamped
          folder will be created in the current directory.
        export_usda: If ``True``, converts each downloaded USD file to USDA and
          saves the additional USDA file in the target folder with the same
          base name as the original USD file.

    Returns:
        File path to the downloaded USD file.
    """
    import datetime
    import os

    import requests

    try:
        from pxr import Usd
    except ImportError as e:
        raise ImportError("Failed to import pxr. Please install USD (e.g. via `pip install usd-core`).") from e

    response = requests.get(url, allow_redirects=True)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to download USD file. Status code: {response.status_code}")
    file = response.content
    dot = os.path.extsep
    base = os.path.basename(url)
    url_folder = os.path.dirname(url)
    base_name = dot.join(base.split(dot)[:-1])
    if target_folder_name is None:
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        target_folder_name = os.path.join(".usd_cache", f"{base_name}_{timestamp}")
    os.makedirs(target_folder_name, exist_ok=True)
    target_filename = os.path.join(target_folder_name, base)
    with open(target_filename, "wb") as f:
        f.write(file)

    stage = Usd.Stage.Open(target_filename, Usd.Stage.LoadNone)
    stage_str = stage.GetRootLayer().ExportToString()
    print(f"Downloaded USD file to {target_filename}.")
    if export_usda:
        usda_filename = os.path.join(target_folder_name, base_name + ".usda")
        with open(usda_filename, "w") as f:
            f.write(stage_str)
            print(f"Exported USDA file to {usda_filename}.")

    # parse referenced USD files like `references = @./franka_collisions.usd@`
    downloaded = set()
    for match in re.finditer(r"references.=.@(.*?)@", stage_str):
        refname = match.group(1)
        if refname.startswith("./"):
            refname = refname[2:]
        if refname in downloaded:
            continue
        try:
            response = requests.get(f"{url_folder}/{refname}", allow_redirects=True)
            if response.status_code != 200:
                print(f"Failed to download reference {refname}. Status code: {response.status_code}")
                continue
            file = response.content
            refdir = os.path.dirname(refname)
            if refdir:
                os.makedirs(os.path.join(target_folder_name, refdir), exist_ok=True)
            ref_filename = os.path.join(target_folder_name, refname)
            if not os.path.exists(ref_filename):
                with open(ref_filename, "wb") as f:
                    f.write(file)
            downloaded.add(refname)
            print(f"Downloaded USD reference {refname} to {ref_filename}.")
            if export_usda:
                ref_stage = Usd.Stage.Open(ref_filename, Usd.Stage.LoadNone)
                ref_stage_str = ref_stage.GetRootLayer().ExportToString()
                base = os.path.basename(ref_filename)
                base_name = dot.join(base.split(dot)[:-1])
                usda_filename = os.path.join(target_folder_name, base_name + ".usda")
                with open(usda_filename, "w") as f:
                    f.write(ref_stage_str)
                    print(f"Exported USDA file to {usda_filename}.")
        except Exception:
            print(f"Failed to download {refname}.")
    return target_filename
