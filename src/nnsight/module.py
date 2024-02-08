"""The Module object acts as the entrypoint into the interwoven intervention graph. Through it, one can manipulate the inputs and outputs of Modules as a model is running. Specifically, the ``.output`` and ``.input`` attributes are the root nodes of the intervention graph, and all operations performed on these Proxy objects are the downstream nodes that populate the graph. Requires a ``Tracer`` object to add the correct proxies to the intervention graph. 

    Examples:

        The example below accesses the inputs and outputs of a gpt2 module, saves them during execution, and prints the resulting values:
        
        .. code-block:: python

            with model.generate() as generator:
                with generator.invoke('The Eiffel Tower is in the city of') as invoker:
                    hidden_states = model.lm_head.input.save()
                    logits = model.lm_head.output.save()

            print(hidden_states.value)
            print(logits.value)

        This example shows how to set the output of a gpt2 module to zero:

        .. code-block:: python

            with model.generate() as generator:
                with generator.invoke('The Eiffel Tower is in the city of') as invoker:
                    model.transformer.h[0].output[0] = 0
"""

from __future__ import annotations

import warnings
from typing import Any, Dict, List, Union

import torch
from torch._guards import detect_fake_mode

from . import util
from .contexts.Tracer import Tracer
from .intervention import InterventionProxy
from .tracing.Graph import Graph
from .tracing.Proxy import Proxy


class Module(torch.nn.Module):
    """Class that wraps existing torch modules within a model's module tree in order to add nnsight functionality.
    Proxies of it's output and input are accessed by `.output` and `.input` respectively.

    Attributes:
        module_path (str): String representing the attribute path of this module relative the the root model. Separated by '.' e.x ('transformer.h.0.mlp'). Set by NNsight on initialization of meta model.
        call_iter (int): Integer representing the current iteration of this module's inputs/outputs.
        meta_outputs (List[torch.Tensor]): List of 'meta' tensors built from the outputs most recent _scan. Is list as there can be multiple shapes for a module called more than once.
        meta_inputs (List[torch.Tensor]): List of 'meta' tensors built from the inputs most recent _scan. Is list as there can be multiple shapes for a module called more than once.
        output (nnsight.intervention.InterventionProxy): Proxy object representing the output of this module. Reset on pass through.
        input (nnsight.intervention.InterventionProxy): Proxy object representing the input of this module. Reset on pass through.
        tracer (nnsight.context.Tracer.Tracer): Object which adds this module's output and input proxies to an intervention graph. Must be set on Module objects manually.
    """

    def __init__(self) -> None:

        self.module_path: str = ""

        self.call_iter = 0

        self.fake_outputs: List[torch.Tensor] = []
        self.fake_inputs: List[torch.Tensor] = []

        self._output: InterventionProxy = None
        self._input: InterventionProxy = None

        self._graph: Graph = None

        self.tracer: Tracer = None

    def reset_proxies(self, propagate: bool = True) -> None:

        self._output: InterventionProxy = None
        self._input: InterventionProxy = None

        if propagate:
            for module in self.modules():
                if isinstance(module, Module):
                    module.reset_proxies(propagate=False)

    def reset(self, propagate: bool = True) -> None:

        self.reset_proxies(propagate=False)

        self.call_iter = 0

        if propagate:
            for module in self.modules():
                if isinstance(module, Module):
                    module.reset(propagate=False)

    def clear(self, propagate: bool = True) -> None:

        self.reset(propagate=False)

        self.fake_outputs = []
        self.fake_inputs = []

        if propagate:
            for module in self.modules():
                if isinstance(module, Module):
                    module.clear(propagate=False)

    def next(self, iteration: int = 1, propagate: bool = False) -> Module:

        self.call_iter += iteration

        self.reset_proxies(propagate=False)

        if propagate:
            for module in self.modules():
                if isinstance(module, Module):
                    module.next(propagate=False)

        return self

    def __call__(self, *args: List[Any], **kwds: Dict[str, Any]) -> Union[Any, Proxy]:
        """Override __call__ to check for Proxy arguments.
        If there are any, we should return an Proxy denoting we want to call the given module with arguments.

        Args:
            args (List[Any]): Positional arguments.
            kwargs (Dict[str,Any]): Keyword arguments

        Returns:
            Union[Any,Proxy]: Either the output of the module if not tracing or a Proxy if tracing.
        """
        proxy: Proxy = None

        def get_proxy(_proxy: Proxy):

            nonlocal proxy

            proxy = _proxy

        util.apply(list(args) + list(kwds.values()), get_proxy, Proxy)

        if proxy is not None:
            module_proxy = getattr(proxy.node.graph.module_proxy, self.module_path)

            return module_proxy.forward(*args, **kwds)

        return super().__call__(*args, **kwds)

    @property
    def output(self) -> InterventionProxy:
        """
        Calling denotes the user wishes to get the output of this module and therefore we create a Proxy of that request.
        Only generates a proxy the first time it is references otherwise return the already set one.

        Returns:
            Proxy: Output proxy.
        """
        if self._output is None:

            if len(self.fake_outputs) == 0:
                fake_output = None
            elif self.call_iter >= len(self.fake_outputs):
                # TODO warning?
                fake_output = self.fake_outputs[-1]
            else:
                fake_output = self.fake_outputs[self.call_iter]

            self._output = self.tracer.graph.add(
                value=fake_output,
                target="argument",
                args=[
                    f"{self.module_path}.output",
                    self.tracer.batch_size,
                    self.tracer.batch_start,
                    self.call_iter,
                ],
            )

        return self._output

    @output.setter
    def output(self, value: Union[InterventionProxy, Any]) -> None:
        """
        Calling denotes the user wishes to set the output of this module and therefore we create a Proxy of that request.

        Args:
            value (Union[Proxy, Any]): Value to set output to.
        """

        self.output.node.graph.add(
            target="swap", args=[self.output.node, value], value=True
        )

        self._output = None

    @property
    def input(self) -> InterventionProxy:
        """
        Calling denotes the user wishes to get the input of this module and therefore we create a Proxy of that request.
        Only generates a proxy the first time it is references otherwise return the already set one.

        Returns:
            Proxy: Input proxy.
        """
        if self._input is None:

            if len(self.fake_inputs) == 0:
                fake_input = None
            elif self.call_iter >= len(self.fake_inputs):
                # TODO warning?
                fake_input = self.fake_inputs[-1]
            else:
                fake_input = self.fake_inputs[self.call_iter]

            self._input = self.tracer.graph.add(
                value=fake_input,
                target="argument",
                args=[
                    f"{self.module_path}.input",
                    self.tracer.batch_size,
                    self.tracer.batch_start,
                    self.call_iter,
                ],
            )

        return self._input

    @input.setter
    def input(self, value: Union[InterventionProxy, Any]) -> None:
        """
        Calling denotes the user wishes to set the input of this module and therefore we create a Proxy of that request.

        Args:
            value (Union[Proxy, Any]): Value to set input to.
        """

        self.input.node.graph.add(
            target="swap", args=[self.input.node, value], value=True
        )

        self._input = None

    @property
    def graph(self) -> Graph:
        if self._graph is None:
            self._graph = Graph.trace(
                self,
                *self.fake_inputs[self.call_iter][0],
                **self.fake_inputs[self.call_iter][1],
            )

        return self._graph

    @staticmethod
    def wrap(module: torch.nn.Module) -> Module:
        """Wraps the torch Module with our Module class. Adds a forward hook to the module to populate output and input data. Does not wrap torch.nn.ModuleList.

        Args:
            module (torch.nn.Module): The torch Module to wrap.

        Returns:
            Module: The wrapped Module.
        """

        def hook(module: Module, input: Any, input_kwargs: Dict, output: Any):

            if detect_fake_mode(input):

                module.reset_proxies()

                input = (input, input_kwargs)

                module.fake_outputs.append(output)
                module.fake_inputs.append(input)

        original_output = getattr(module, "output", None)
        original_input = getattr(module, "input", None)
        original_cls = type(module)

        util.wrap(module, Module)

        if original_output is not None or original_input is not None:
            warnings.warn(
                f"Module of type `{original_cls}` has pre-defined either an `output` attribute or an `input` attribute. nnsight input and output access will be mounted at `.nns_output` and `.nns_input instead of `.output` and `.input` respectively for this module only."
            )

            module_cls = Module

            new_module_cls = type(f"{module_cls.__name__}.Preserved", (module_cls,), {})

            nns_output = getattr(new_module_cls, "output")
            nns_input = getattr(new_module_cls, "input")

            setattr(new_module_cls, "nns_output", nns_output)
            setattr(new_module_cls, "output", original_output)

            setattr(new_module_cls, "nns_input", nns_input)
            setattr(new_module_cls, "input", original_input)

            new_cls = type(
                f"{module.__class__.__name__}.Preserved",
                (
                    new_module_cls,
                    module.__class__.__bases__[1],
                ),
                {},
            )

            module.__class__ = new_cls

        module.register_forward_hook(hook, with_kwargs=True)

        return module
