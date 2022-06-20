# This file is part of asyncmd.
#
# asyncmd is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# asyncmd is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with asyncmd. If not, see <https://www.gnu.org/licenses/>.
import asyncio
import inspect
import logging
import functools
import typing
import numpy as np
from concurrent.futures import ThreadPoolExecutor

from .._config import _SEMAPHORES
from .trajectory import Trajectory
from .functionwrapper import TrajectoryFunctionWrapper
from .convert import TrajectoryConcatenator
from ..utils import get_all_traj_parts, nstout_from_mdconfig


logger = logging.getLogger(__name__)


class MaxStepsReachedError(Exception):
    """
    Error raised when the simulation terminated because the (user-defined)
    maximum number of integration steps/trajectory frames has been reached.
    """
    pass


# TODO: move to trajectory.convert?
async def construct_TP_from_plus_and_minus_traj_segments(
                                minus_trajs: "list[Trajectory]",
                                minus_state: int,
                                plus_trajs: "list[Trajectory]",
                                plus_state: int,
                                state_funcs: "list[TrajectoryFunctionWrapper]",
                                tra_out: str,
                                struct_out: typing.Optional[str] = None,
                                overwrite: bool = False
                                                         ) -> Trajectory:
    """
    Construct a continous TP from plus and minus segments until states.

    This is used e.g. in TwoWay TPS or if you try to get TPs out of a committor
    simulation. Note, that this inverts all velocities on the minus segments.

    Parameters
    ----------
    minus_trajs : list[Trajectory]
        Trajectories that go "backward in time", these are going to be inverted
    minus_state : int
        Index (in ``state_funcs``) of the first state reached on minus trajs.
    plus_trajs : list[Trajectory]
        Trajectories that go "forward in time", these are taken as is.
    plus_state : int
        Index (in ``state_funcs``) of the first state reached on plus trajs.
    state_funcs : list[TrajectoryFunctionWrapper]
        List of wrapped state functions, the indices to the states must match
        the minus and plus state indices!
    tra_out : str
        Absolute or relative path to the output trajectory file.
    struct_out : str, optional
        Absolute or relative path to the output structure file, if None we will
        use the structure file of the first minus_traj, by default None.
    overwrite : bool, optional
        Whether we should overwrite tra_out if it exists, by default False.

    Returns
    -------
    Trajectory
        The constructed transition.
    """
    # first find the slices to concatenate
    # minus state first
    minus_state_vals = await asyncio.gather(*(state_funcs[minus_state](t)
                                              for t in minus_trajs)
                                            )
    part_lens = [len(v) for v in minus_state_vals]
    # make it into one long array
    minus_state_vals = np.concatenate(minus_state_vals, axis=0)
    # get the first frame in state (np.where always returns a tuple)
    frames_in_minus, = np.where(minus_state_vals)
    # get the first frame in minus state in minus_trajs, this will become the
    # first frame of the traj since we invert this part
    first_frame_in_minus = np.min(frames_in_minus)
    # I think this is overkill, i.e. we can always expect that
    # first frame in state is in last part?!
    # [this could potentially make this a bit shorter and maybe
    #  even a bit more readable :)]
    # But for now: better be save than sorry :)
    # find the first part in which minus state is reached, i.e. the last one
    # to take when constructing the TP
    last_part_idx = 0
    frame_sum = part_lens[last_part_idx]
    while first_frame_in_minus >= frame_sum:
        last_part_idx += 1
        frame_sum += part_lens[last_part_idx]
    # find the first frame in state (counting from start of last part to take)
    _first_frame_in_minus = (first_frame_in_minus
                             - sum(part_lens[:last_part_idx]))  # >= 0
    # now construct the slices and trajs list (backwards!)
    # the last/first part
    slices = [(_first_frame_in_minus, None, -1)]  # negative stride!
    trajs = [minus_trajs[last_part_idx]]
    # the ones we take fully (if any) [the range looks a bit strange
    # because we dont take last_part_index but include the zero as idx]
    slices += [(-1, None, -1) for _ in range(last_part_idx - 1, -1, -1)]
    trajs += [minus_trajs[i] for i in range(last_part_idx - 1, -1, -1)]

    # now plus trajectories, i.e. the part we put in positive stride
    plus_state_vals = await asyncio.gather(*(state_funcs[plus_state](t)
                                             for t in plus_trajs)
                                           )
    part_lens = [len(v) for v in plus_state_vals]
    # make it into one long array
    plus_state_vals = np.concatenate(plus_state_vals, axis=0)
    # get the first frame in state
    frames_in_plus, = np.where(plus_state_vals)
    first_frame_in_plus = np.min(frames_in_plus)
    # find the part
    last_part_idx = 0
    frame_sum = part_lens[last_part_idx]
    while first_frame_in_plus >= frame_sum:
        last_part_idx += 1
        frame_sum += part_lens[last_part_idx]
    # find the first frame in state (counting from start of last part)
    _first_frame_in_plus = (first_frame_in_plus
                            - sum(part_lens[:last_part_idx]))  # >= 0
    # construct the slices and add trajs to list (forward!)
    # NOTE: here we exclude the starting configuration, i.e. the SP,
    #       such that it is in the concatenated trajectory only once!
    #       (gromacs has the first frame in the trajectory)
    if last_part_idx > 0:
        # these are the trajectory segments we take completely
        # [this excludes last_part_idx so far]
        slices += [(1, None, 1)]
        trajs += [plus_trajs[0]]
        # these will be empty if last_part_idx < 2
        slices += [(0, None, 1) for _ in range(1, last_part_idx)]
        trajs += [plus_trajs[i] for i in range(1, last_part_idx)]
        # add last part (with the last frame as first frame in plus state)
        slices += [(0, _first_frame_in_plus + 1, 1)]
        trajs += [plus_trajs[last_part_idx]]
    else:
        # first and last part is the same, so exclude starting configuration
        # from the same segment that has the last frame as first frame in plus
        slices += [(1, _first_frame_in_plus + 1, 1)]
        trajs += [plus_trajs[last_part_idx]]
    # finally produce the concatenated path
    concat = functools.partial(TrajectoryConcatenator().concatenate,
                               trajs=trajs,
                               slices=slices,
                               tra_out=tra_out,
                               struct_out=struct_out,
                               overwrite=overwrite)
    loop = asyncio.get_running_loop()
    async with _SEMAPHORES["MAX_PROCESS"]:
        with ThreadPoolExecutor(max_workers=1,
                                thread_name_prefix="concat_thread",
                                ) as pool:
            path_traj = await loop.run_in_executor(pool, concat)
    return path_traj


# TODO: DOCUMENT
class ConditionalTrajectoryPropagator:
    """
    Propagate a trajectory until any of the given conditions is fullfilled.

    This class propagates the trajectory using a given MD engine (class) in
    small chunks (chunksize is determined by walltime_per_part) and checks
    after every chunk is done if any condition has been fullfilled.
    It then returns a list of trajectory parts and the index of the condition
    first fullfilled. It can also concatenate the parts into one trajectory,
    which then starts with the starting configuration and ends with the frame
    fullfilling the condition.

    Attributes
    ----------
    conditions : list[callable]
        List of (wrapped) condition functions.

    Notes
    -----
    We assume that every condition function returns a list/ a 1d array with
    True or False for each frame, i.e. if we fullfill condition at any given
    frame.
    We assume non-overlapping conditions, i.e. a configuration can not fullfill
    two conditions at the same time, **it is the users responsibility to ensure
    that their conditions are sane**.
    """

    # NOTE: we assume that every condition function returns a list/ a 1d array
    #       with True/False for each frame, i.e. if we fullfill condition at
    #       any given frame
    # NOTE: we assume non-overlapping conditions, i.e. a configuration can not
    #       fullfill two conditions at the same time, it is the users
    #       responsibility to ensure that their conditions are sane

    def __init__(self, conditions, engine_cls,
                 engine_kwargs: dict,
                 walltime_per_part: float,
                 max_steps: typing.Optional[int] = None,
                 max_frames: typing.Optional[int] = None,
                 ):
        """
        Initialize a `ConditionalTrajectoryPropagator`.

        Parameters
        ----------
        conditions : list[callable], usually list[TrajectoryFunctionWrapper]
            List of condition functions, usually wrapped function for
            asyncronous application, but can be any callable that takes a
            :class:`asyncmd.Trajectory` and returns an array of True and False
            values (one value per frame).
        engine_cls : type(asyncmd.mdengine.MDEngine)
            Class of the MD engine to use, **uninitialized!**
        engine_kwargs : dict
            Dictionary of key word arguments to initialize the MD engine.
        walltime_per_part : float
            Walltime per trajectory segment, in hours.
        max_steps : int, optional
            Maximum number of integration steps to do before stopping the
            simulation because it did not commit to any condition,
            by default None. Takes precendence over max_frames if both given.
        max_frames : int, optional
            Maximum number of frames to produce before stopping the simulation
            because it did not commit to any condition, by default None.

        Notes
        -----
        ``max_steps`` and ``max_frames`` are redundant since
        ``max_steps = max_frames * output_frequency``, if both are given
        max_steps takes precedence.
        """
        self._conditions = None
        self._condition_func_is_coroutine = None
        self.conditions = conditions
        self.engine_cls = engine_cls
        self.engine_kwargs = engine_kwargs
        self.walltime_per_part = walltime_per_part
        # find nstout
        try:
            traj_type = engine_kwargs["output_traj_type"]
        except KeyError:
            # not in there so it will be the engine default
            traj_type = engine_cls.output_traj_type
        nstout = nstout_from_mdconfig(mdconfig=engine_kwargs["mdconfig"],
                                      output_traj_type=traj_type)
        # sort out if we use max_frames or max_steps
        if max_frames is not None and max_steps is not None:
            logger.warning("Both max_steps and max_frames given. Note that "
                           + "max_steps will take precedence.")
        if max_steps is not None:
            self.max_steps = max_steps
        elif max_frames is not None:
            self.max_steps = max_frames * nstout
        else:
            logger.info("Neither max_frames nor max_steps given. "
                        + "Setting max_steps to infinity.")
            # this is a float but can be compared to ints
            self.max_steps = np.inf

    # TODO/FIXME: self._conditions is a list...that means users can change
    #            single elements without using the setter!
    #            we could use a list subclass as for the MDconfig?!
    @property
    def conditions(self):
        return self._conditions

    @conditions.setter
    def conditions(self, conditions):
        # I think it is save to assume each condition has a .__call__() method?
        # so we just check if it is awaitable
        self._condition_func_is_coroutine = [
                                        inspect.iscoroutinefunction(c.__call__)
                                        for c in conditions
                                             ]
        if not all(self._condition_func_is_coroutine):
            # and warn if it is not
            logger.warning(
                    "It is recommended to use coroutinefunctions for all "
                    + "conditions. This can easily be achieved by wrapping any"
                    + " function in a TrajectoryFunctionWrapper. All "
                    + "non-coroutine condition functions will be blocking when"
                    + " applied! ([c is coroutine for c in conditions] = "
                    + f"{self._condition_func_is_coroutine})"
                           )
        self._conditions = conditions

    async def propagate_and_concatenate(self,
                                        starting_configuration: Trajectory,
                                        workdir: str,
                                        deffnm: str,
                                        tra_out: str,
                                        overwrite: bool = False,
                                        continuation: bool = False
                                        ) -> tuple[Trajectory, int]:
        """
        Chain :meth:`propagate` and :meth:`cut_and_concatenate` methods.

        Parameters
        ----------
        starting_configuration : Trajectory
            The configuration (including momenta) to start MD from.
        workdir : str
            Absolute or relative path to the working directory.
        deffnm : str
            MD engine deffnm for trajectory parts and other files.
        tra_out : str
            Absolute or relative path for the concatenated output trajectory.
        overwrite : bool, optional
            Whether the output trajectory should be overwritten if it exists,
            by default False.
        continuation : bool, optional
            Whether we are continuing a previous MD run (with the same deffnm
            and working directory), by default False.

        Returns
        -------
        (traj_out, idx_of_condition_fullfilled) : (Trajectory, int)
            The concatenated output trajectory from starting configuration
            until the first condition is True and the index to the condition
            function in `conditions`.

        Raises
        ------
        MaxStepsReachedError
            When the defined maximum number of integration steps/trajectory
            frames has been reached in :meth:`propagate`.
        """
        # this just chains propagate and cut_and_concatenate
        # usefull for committor simulations, for e.g. TPS one should try to
        # directly concatenate both directions to a full TP if possible
        trajs, first_condition_fullfilled = await self.propagate(
                                starting_configuration=starting_configuration,
                                workdir=workdir,
                                deffnm=deffnm,
                                continuation=continuation
                                                              )
        # NOTE: it should not matter too much speedwise that we recalculate
        #       the condition functions, they are expected to be wrapped funcs
        #       i.e. the second time we should just get the values from cache
        full_traj, first_condition_fullfilled = await self.cut_and_concatenate(
                                                        trajs=trajs,
                                                        tra_out=tra_out,
                                                        overwrite=overwrite,
                                                                            )
        return full_traj, first_condition_fullfilled

    async def propagate(self,
                        starting_configuration: Trajectory,
                        workdir: str,
                        deffnm: str,
                        continuation: bool = False,
                        ) -> tuple[list[Trajectory], int]:
        """
        Propagate the trajectory until any condition is fullfilled.

        Return a list of trajecory segments and the first condition fullfilled.

        Parameters
        ----------
        starting_configuration : Trajectory
            The configuration (including momenta) to start MD from.
        workdir : str
            Absolute or relative path to the working directory.
        deffnm : str
            MD engine deffnm for trajectory parts and other files.
        continuation : bool, optional
            Whether we are continuing a previous MD run (with the same deffnm
            and working directory), by default False.

        Returns
        -------
        (traj_segments, idx_of_condition_fullfilled) : (list[Trajectory], int)
            List of trajectory (segements), the last entry is the one on which
            the first condition is fullfilled at some frame, the ineger is the
            index to the condition function in `conditions`.

        Raises
        ------
        MaxStepsReachedError
            When the defined maximum number of integration steps/trajectory
            frames has been reached.
        """
        # NOTE: curently this just returns a list of trajs + the condition
        #       fullfilled
        #       this feels a bit uncomfortable but avoids that we concatenate
        #       everything a quadrillion times when we use the results
        # check first if the start configuration is fullfilling any condition
        cond_vals = await self._condition_vals_for_traj(starting_configuration)
        if np.any(cond_vals):
            conds_fullfilled, frame_nums = np.where(cond_vals)
            # gets the frame with the lowest idx where any condition is True
            min_idx = np.argmin(frame_nums)
            first_condition_fullfilled = conds_fullfilled[min_idx]
            logger.error(f"Starting configuration ({starting_configuration}) "
                         + "is already fullfilling the condition with idx"
                         + f" {first_condition_fullfilled}.")
            # we just return the starting configuration/trajectory
            trajs = [starting_configuration]
            return trajs, first_condition_fullfilled

        # starting configuration does not fullfill any condition, lets do MD
        engine = self.engine_cls(**self.engine_kwargs)
        if not continuation:
            # no continuation, just prepare the engine from scratch
            await engine.prepare(
                        starting_configuration=starting_configuration,
                        workdir=workdir,
                        deffnm=deffnm,
                                )
            any_cond_fullfilled = False
            trajs = []
            step_counter = 0
        else:
            # continuation: get all traj parts already done and continue from
            # there, i.e. append to the last traj part found
            # NOTE: we assume that the condition functions could be different
            # so get all traj parts and calculate the condition funcs on them
            trajs = get_all_traj_parts(folder=workdir, deffnm=deffnm,
                                       engine=engine)
            cond_vals = await asyncio.gather(
                            *(self._condition_vals_for_traj(t) for t in trajs)
                                             )
            cond_vals = np.concatenate([np.asarray(s) for s in cond_vals],
                                       axis=1)
            # see if we already fullfill a condition on the existing traj parts
            any_cond_fullfilled = np.any(cond_vals)
            if any_cond_fullfilled:
                conds_fullfilled, frame_nums = np.where(cond_vals)
                # gets the frame with the lowest idx where any cond is True
                min_idx = np.argmin(frame_nums)
                first_condition_fullfilled = conds_fullfilled[min_idx]
                # already fullfill a condition, get out of here!
                return trajs, first_condition_fullfilled
                # Did not fullfill any condition yet, so prepare the engine to
                # continue the simulation until we reach any of the (new) conds
            await engine.prepare_from_files(workdir=workdir, deffnm=deffnm)
            step_counter = engine.steps_done

        while ((not any_cond_fullfilled)
                and (step_counter <= self.max_steps)):
            traj = await engine.run_walltime(self.walltime_per_part)
            cond_vals = await self._condition_vals_for_traj(traj)
            any_cond_fullfilled = np.any(cond_vals)
            step_counter = engine.steps_done
            trajs.append(traj)
        if not any_cond_fullfilled:
            # left while loop because of max_frames reached
            raise MaxStepsReachedError(
                f"Engine produced {step_counter} steps (>= {self.max_steps})."
                                       )
        # cond_vals are the ones for the last traj
        # here we get which conditions are True and at which frame
        conds_fullfilled, frame_nums = np.where(cond_vals)
        # gets the frame with the lowest idx where any condition is True
        min_idx = np.argmin(frame_nums)
        # and now the idx to self.conditions for cond that was first fullfilled
        # NOTE/FIXME: if two conditions are reached simultaneously at min_idx,
        #       this will find the condition with the lower idx only
        first_condition_fullfilled = conds_fullfilled[min_idx]
        return trajs, first_condition_fullfilled

    async def cut_and_concatenate(self,
                                  trajs: list[Trajectory],
                                  tra_out: str,
                                  overwrite: bool = False,
                                  ) -> tuple[Trajectory, int]:
        """
        Cut and concatenate the trajectory until the first condition is True.

        Take a list of trajectory segments and form one continous trajectory
        until the first frame that fullfills any condition. The expected input
        is a list of trajectories, e.g. the output of the :meth:`propagate`
        method.

        Parameters
        ----------
        trajs : list[Trajectory]
            Trajectory segments to cut and concatenate.
        tra_out : str
            Absolute or relative path for the concatenated output trajectory.
        overwrite : bool, optional
            Whether the output trajectory should be overwritten if it exists,
            by default False.

        Returns
        -------
        (traj_out, idx_of_condition_fullfilled) : (Trajectory, int)
            The concatenated output trajectory from starting configuration
            until the first condition is True and the index to the condition
            function in `conditions`.
        """
        # trajs is a list of trajectories, e.g. the return of propagate
        # tra_out and overwrite are passed directly to the Concatenator
        # NOTE: we assume that frame0 of traj0 does not fullfill any condition
        #       and return only the subtrajectory from frame0 until any
        #       condition is first True (the rest is ignored)
        # get all func values and put them into one big array
        cond_vals = await asyncio.gather(
                            *(self._condition_vals_for_traj(t) for t in trajs)
                                         )
        # cond_vals is a list (trajs) of lists (conditions)
        # take condition 0 (always present) to get the traj part lengths
        part_lens = [len(c[0]) for c in cond_vals]  # c[0] is 1d (np)array
        cond_vals = np.concatenate([np.asarray(c) for c in cond_vals],
                                   axis=1)
        conds_fullfilled, frame_nums = np.where(cond_vals)
        # gets the frame with the lowest idx where any condition is True
        min_idx = np.argmin(frame_nums)
        first_condition_fullfilled = conds_fullfilled[min_idx]
        first_frame_in_cond = frame_nums[min_idx]
        # find out in which part it is
        last_part_idx = 0
        frame_sum = part_lens[last_part_idx]
        while first_frame_in_cond >= frame_sum:
            last_part_idx += 1
            frame_sum += part_lens[last_part_idx]
        # find the first frame in cond (counting from start of last part)
        _first_frame_in_cond = (first_frame_in_cond
                                - sum(part_lens[:last_part_idx]))  # >= 0
        if last_part_idx > 0:
            # trajectory parts which we take fully
            slices = [(0, None, 1) for _ in range(last_part_idx)]
        else:
            # only the first/last part
            slices = []
        # and the last part until including first_frame_in_cond
        slices += [(0, _first_frame_in_cond + 1, 1)]
        # we fill in all args as kwargs because there are so many
        concat = functools.partial(TrajectoryConcatenator().concatenate,
                                   trajs=trajs[:last_part_idx + 1],
                                   slices=slices,
                                   # take the structure file of the traj, as it
                                   # comes from the engine directly
                                   tra_out=tra_out, struct_out=None,
                                   overwrite=overwrite)
        loop = asyncio.get_running_loop()
        async with _SEMAPHORES["MAX_PROCESS"]:
            with ThreadPoolExecutor(max_workers=1,
                                    thread_name_prefix="concat_thread",
                                    ) as pool:
                full_traj = await loop.run_in_executor(pool, concat)
        return full_traj, first_condition_fullfilled

    async def _condition_vals_for_traj(self, traj):
        # return a list of condition_func results,
        # one for each condition func in conditions
        if all(self._condition_func_is_coroutine):
            # easy, all coroutines
            return await asyncio.gather(*(c(traj) for c in self.conditions))
        elif not any(self._condition_func_is_coroutine):
            # also easy (but blocking), none is coroutine
            return [c(traj) for c in self.conditions]
        else:
            # need to piece it together
            # first the coroutines concurrently
            coros = [c(traj) for c, c_is_coro
                     in zip(self.conditions, self._condition_func_is_coroutine)
                     if c_is_coro
                     ]
            coro_res = await asyncio.gather(*coros)
            # now either take the result from coro execution or calculate it
            all_results = []
            for c, c_is_coro in zip(self.conditions,
                                    self._condition_func_is_coroutine):
                if c_is_coro:
                    all_results.append(coro_res.pop(0))
                else:
                    all_results.append(c(traj))
            return all_results
            # NOTE: this would be elegant, but to_thread() is py v>=3.9
            # we wrap the non-coroutines into tasks to schedule all together
            #all_conditions_as_coro = [
            #    c(traj) if c_is_cor else asyncio.to_thread(c, traj)
            #    for c, c_is_cor in zip(self.conditions, self._condition_func_is_coroutine)
            #                      ]
            #return await asyncio.gather(*all_conditions_as_coro)


# alias for people coming from the path sampling community :)
TrajectoryPropagatorUntilAnyState = ConditionalTrajectoryPropagator
