"""Helpers for creating schema based data entry flows."""
from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Mapping
import copy
from dataclasses import dataclass
import types
from typing import Any, cast

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback, split_entity_id
from homeassistant.data_entry_flow import FlowResult, UnknownHandler

from . import entity_registry as er, selector


class SchemaFlowError(Exception):
    """Validation failed."""


@dataclass
class SchemaFlowStep:
    """Define a config or options flow step."""


@dataclass
class SchemaFlowFormStep(SchemaFlowStep):
    """Define a config or options flow form step."""

    schema: vol.Schema | Callable[
        [SchemaConfigFlowHandler | SchemaOptionsFlowHandler, dict[str, Any]],
        vol.Schema | None,
    ] | None
    """Optional voluptuous schema, or function which returns a schema or None, for
    requesting and validating user input.

    - If a function is specified, the function will be passed the handler, which is
    either an instance of SchemaConfigFlowHandler or SchemaOptionsFlowHandler, and the
    union of config entry options and user input from previous steps.
    - If schema validation fails, the step will be retried. If the schema is None, no
    user input is requested.
    """

    validate_user_input: Callable[[dict[str, Any]], dict[str, Any]] = lambda x: x
    """Optional function to validate user input.

    - The `validate_user_input` function is called if the schema validates successfully.
    - The `validate_user_input` function is passed the user input from the current step.
    - The `validate_user_input` should raise `SchemaFlowError` is user input is invalid.
    """

    next_step: Callable[[dict[str, Any]], str] | str | None = None
    """Optional property to identify next step.

    - If `next_step` is a function, it is called if the schema validates successfully or
    if no schema is defined. The `next_step` function is passed the union of config entry
    options and user input from previous steps.
    - If `next_step` is None, the flow is ended with `FlowResultType.CREATE_ENTRY`.
    """


@dataclass
class SchemaFlowMenuStep(SchemaFlowStep):
    """Define a config or options flow menu step."""

    # Menu options
    options: list[str] | dict[str, str]


class SchemaCommonFlowHandler:
    """Handle a schema based config or options flow."""

    def __init__(
        self,
        handler: SchemaConfigFlowHandler | SchemaOptionsFlowHandler,
        flow: Mapping[str, SchemaFlowStep],
        options: dict[str, Any] | None,
    ) -> None:
        """Initialize a common handler."""
        self._flow = flow
        self._handler = handler
        self._options = options if options is not None else {}

    async def async_step(
        self, step_id: str, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a step."""
        if isinstance(self._flow[step_id], SchemaFlowFormStep):
            return await self._async_form_step(step_id, user_input)
        return await self._async_menu_step(step_id, user_input)

    def _get_schema(
        self, form_step: SchemaFlowFormStep, options: dict[str, Any]
    ) -> vol.Schema | None:
        if form_step.schema is None:
            return None
        if isinstance(form_step.schema, vol.Schema):
            return form_step.schema
        return form_step.schema(self._handler, options)

    async def _async_form_step(
        self, step_id: str, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a form step."""
        form_step: SchemaFlowFormStep = cast(SchemaFlowFormStep, self._flow[step_id])

        if (
            user_input is not None
            and (data_schema := self._get_schema(form_step, self._options))
            and data_schema.schema
            and not self._handler.show_advanced_options
        ):
            # Add advanced field default if not set
            for key in data_schema.schema.keys():
                if isinstance(key, (vol.Optional, vol.Required)):
                    if (
                        key.description
                        and key.description.get("advanced")
                        and key.default is not vol.UNDEFINED
                        and key not in self._options
                    ):
                        user_input[str(key.schema)] = key.default()

        if user_input is not None and form_step.schema is not None:
            # Do extra validation of user input
            try:
                user_input = form_step.validate_user_input(user_input)
            except SchemaFlowError as exc:
                return self._show_next_step(step_id, exc, user_input)

        if user_input is not None:
            # User input was validated successfully, update options
            self._options.update(user_input)

        next_step_id: str = step_id
        if user_input is not None or form_step.schema is None:
            # Get next step
            if form_step.next_step is None:
                # Flow done, create entry or update config entry options
                return self._handler.async_create_entry(data=self._options)

            if isinstance(form_step.next_step, str):
                next_step_id = form_step.next_step
            else:
                next_step_id = form_step.next_step(self._options)

        return self._show_next_step(next_step_id)

    def _show_next_step(
        self,
        next_step_id: str,
        error: SchemaFlowError | None = None,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Show form for next step."""
        options = dict(self._options)
        if user_input:
            options.update(user_input)

        if isinstance(self._flow[next_step_id], SchemaFlowMenuStep):
            menu_step = cast(SchemaFlowMenuStep, self._flow[next_step_id])
            return self._handler.async_show_menu(
                step_id=next_step_id,
                menu_options=menu_step.options,
            )

        form_step = cast(SchemaFlowFormStep, self._flow[next_step_id])

        if (
            data_schema := self._get_schema(form_step, self._options)
        ) and data_schema.schema:
            # Make a copy of the schema with suggested values set to saved options
            schema = {}
            for key, val in data_schema.schema.items():

                if isinstance(key, vol.Marker):
                    # Exclude advanced field
                    if (
                        key.description
                        and key.description.get("advanced")
                        and not self._handler.show_advanced_options
                    ):
                        continue

                new_key = key
                if key in options and isinstance(key, vol.Marker):
                    # Copy the marker to not modify the flow schema
                    new_key = copy.copy(key)
                    new_key.description = {"suggested_value": options[key]}
                schema[new_key] = val
            data_schema = vol.Schema(schema)

        errors = {"base": str(error)} if error else None

        # Show form for next step
        return self._handler.async_show_form(
            step_id=next_step_id,
            data_schema=data_schema,
            errors=errors,
            last_step=form_step.next_step is None,
        )

    async def _async_menu_step(
        self, step_id: str, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a menu step."""
        menu_step: SchemaFlowMenuStep = cast(SchemaFlowMenuStep, self._flow[step_id])
        return self._handler.async_show_menu(
            step_id=step_id,
            menu_options=menu_step.options,
        )


class SchemaConfigFlowHandler(config_entries.ConfigFlow):
    """Handle a schema based config flow."""

    config_flow: Mapping[str, SchemaFlowStep]
    options_flow: Mapping[str, SchemaFlowStep] | None = None

    VERSION = 1

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Initialize a subclass."""
        super().__init_subclass__(**kwargs)

        @callback
        def _async_get_options_flow(
            config_entry: config_entries.ConfigEntry,
        ) -> config_entries.OptionsFlow:
            """Get the options flow for this handler."""
            if cls.options_flow is None:
                raise UnknownHandler

            return SchemaOptionsFlowHandler(
                config_entry, cls.options_flow, cls.async_options_flow_finished
            )

        # Create an async_get_options_flow method
        cls.async_get_options_flow = _async_get_options_flow  # type: ignore[assignment]

        # Create flow step methods for each step defined in the flow schema
        for step in cls.config_flow:
            setattr(cls, f"async_step_{step}", cls._async_step(step))

    def __init__(self) -> None:
        """Initialize config flow."""
        self._common_handler = SchemaCommonFlowHandler(self, self.config_flow, None)

    @classmethod
    @callback
    def async_supports_options_flow(
        cls, config_entry: config_entries.ConfigEntry
    ) -> bool:
        """Return options flow support for this handler."""
        return cls.options_flow is not None

    @staticmethod
    def _async_step(step_id: str) -> Callable:
        """Generate a step handler."""

        async def _async_step(
            self: SchemaConfigFlowHandler, user_input: dict[str, Any] | None = None
        ) -> FlowResult:
            """Handle a config flow step."""
            # pylint: disable-next=protected-access
            result = await self._common_handler.async_step(step_id, user_input)
            return result

        return _async_step

    @abstractmethod
    @callback
    def async_config_entry_title(self, options: Mapping[str, Any]) -> str:
        """Return config entry title.

        The options parameter contains config entry options, which is the union of user
        input from the config flow steps.
        """

    @callback
    def async_config_flow_finished(self, options: Mapping[str, Any]) -> None:
        """Take necessary actions after the config flow is finished, if needed.

        The options parameter contains config entry options, which is the union of user
        input from the config flow steps.
        """

    @callback
    @staticmethod
    def async_options_flow_finished(
        hass: HomeAssistant, options: Mapping[str, Any]
    ) -> None:
        """Take necessary actions after the options flow is finished, if needed.

        The options parameter contains config entry options, which is the union of stored
        options and user input from the options flow steps.
        """

    @callback
    def async_create_entry(  # pylint: disable=arguments-differ
        self,
        data: Mapping[str, Any],
        **kwargs: Any,
    ) -> FlowResult:
        """Finish config flow and create a config entry."""
        self.async_config_flow_finished(data)
        return super().async_create_entry(
            data={}, options=data, title=self.async_config_entry_title(data), **kwargs
        )


class SchemaOptionsFlowHandler(config_entries.OptionsFlowWithConfigEntry):
    """Handle a schema based options flow."""

    def __init__(
        self,
        config_entry: config_entries.ConfigEntry,
        options_flow: Mapping[str, SchemaFlowStep],
        async_options_flow_finished: Callable[[HomeAssistant, Mapping[str, Any]], None]
        | None = None,
    ) -> None:
        """Initialize options flow.

        If needed, `async_options_flow_finished` can be set to take necessary actions
        after the options flow is finished. The second parameter contains config entry
        options, which is the union of stored options and user input from the options
        flow steps.
        """
        super().__init__(config_entry)
        self._common_handler = SchemaCommonFlowHandler(
            self, options_flow, self._options
        )
        self._async_options_flow_finished = async_options_flow_finished

        for step in options_flow:
            setattr(
                self,
                f"async_step_{step}",
                types.MethodType(self._async_step(step), self),
            )

    @staticmethod
    def _async_step(step_id: str) -> Callable:
        """Generate a step handler."""

        async def _async_step(
            self: SchemaConfigFlowHandler, user_input: dict[str, Any] | None = None
        ) -> FlowResult:
            """Handle an options flow step."""
            # pylint: disable-next=protected-access
            result = await self._common_handler.async_step(step_id, user_input)
            return result

        return _async_step

    @callback
    def async_create_entry(  # pylint: disable=arguments-differ
        self,
        data: Mapping[str, Any],
        **kwargs: Any,
    ) -> FlowResult:
        """Finish config flow and create a config entry."""
        if self._async_options_flow_finished:
            self._async_options_flow_finished(self.hass, data)
        return super().async_create_entry(title="", data=data, **kwargs)


@callback
def wrapped_entity_config_entry_title(
    hass: HomeAssistant, entity_id_or_uuid: str
) -> str:
    """Generate title for a config entry wrapping a single entity.

    If the entity is registered, use the registry entry's name.
    If the entity is in the state machine, use the name from the state.
    Otherwise, fall back to the object ID.
    """
    registry = er.async_get(hass)
    entity_id = er.async_validate_entity_id(registry, entity_id_or_uuid)
    object_id = split_entity_id(entity_id)[1]
    entry = registry.async_get(entity_id)
    if entry:
        return entry.name or entry.original_name or object_id
    state = hass.states.get(entity_id)
    if state:
        return state.name or object_id
    return object_id


@callback
def entity_selector_without_own_entities(
    handler: SchemaOptionsFlowHandler,
    entity_selector_config: selector.EntitySelectorConfig,
) -> vol.Schema:
    """Return an entity selector which excludes own entities."""
    entity_registry = er.async_get(handler.hass)
    entities = er.async_entries_for_config_entry(
        entity_registry,
        handler.config_entry.entry_id,
    )
    entity_ids = [ent.entity_id for ent in entities]

    final_selector_config = entity_selector_config.copy()
    final_selector_config["exclude_entities"] = entity_ids

    return selector.EntitySelector(final_selector_config)
