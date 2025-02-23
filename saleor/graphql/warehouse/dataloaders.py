from collections import defaultdict
from typing import DefaultDict, Iterable, List, Optional, Tuple
from uuid import UUID

from django.conf import settings
from django.db.models import Exists, OuterRef

from ...channel.models import Channel
from ...warehouse.models import ShippingZone, Stock, Warehouse
from ..account.dataloaders import AddressByIdLoader
from ..channel.dataloaders import ChannelBySlugLoader
from ..core.dataloaders import DataLoader

CountryCode = Optional[str]
VariantIdCountryCodeChannelSlug = Tuple[int, CountryCode, str]


class AvailableQuantityByProductVariantIdCountryCodeAndChannelSlugLoader(
    DataLoader[VariantIdCountryCodeChannelSlug, int]
):
    """Calculates available variant quantity based on variant ID and country code.

    For each country code, for each shipping zone supporting that country,
    calculate the maximum available quantity, then return either that number
    or the maximum allowed checkout quantity, whichever is lower.
    """

    context_key = "available_quantity_by_productvariant_and_country"

    def batch_load(self, keys):
        # Split the list of keys by country first. A typical query will only touch
        # a handful of unique countries but may access thousands of product variants
        # so it's cheaper to execute one query per country.
        variants_by_country_and_channel: DefaultDict[
            CountryCode, List[int]
        ] = defaultdict(list)
        for variant_id, country_code, channel_slug in keys:
            variants_by_country_and_channel[(country_code, channel_slug)].append(
                variant_id
            )

        # For each country code execute a single query for all product variants.
        quantity_by_variant_and_country: DefaultDict[
            VariantIdCountryCodeChannelSlug, int
        ] = defaultdict(int)
        for key, variant_ids in variants_by_country_and_channel.items():
            country_code, channel_slug = key
            quantities = self.batch_load_quantities_by_country(
                country_code, channel_slug, variant_ids
            )
            for variant_id, quantity in quantities:
                quantity_by_variant_and_country[
                    (variant_id, country_code, channel_slug)
                ] = quantity

        return [quantity_by_variant_and_country[key] for key in keys]

    def batch_load_quantities_by_country(
        self,
        country_code: Optional[CountryCode],
        channel_slug: Optional[str],
        variant_ids: Iterable[int],
    ) -> Iterable[Tuple[int, int]]:
        # get stocks only for warehouses assigned to the shipping zones
        # that are available in the given channel
        stocks = Stock.objects.filter(product_variant_id__in=variant_ids)
        WarehouseShippingZone = Warehouse.shipping_zones.through  # type: ignore
        warehouse_shipping_zones = WarehouseShippingZone.objects.all()
        additional_warehouse_filter = False
        if country_code or channel_slug:
            additional_warehouse_filter = True
            if country_code:
                shipping_zones = ShippingZone.objects.filter(
                    countries__contains=country_code
                ).values("pk")
                warehouse_shipping_zones = warehouse_shipping_zones.filter(
                    Exists(shipping_zones.filter(pk=OuterRef("shippingzone_id")))
                )
            if channel_slug:
                ShippingZoneChannel = Channel.shipping_zones.through  # type: ignore
                channels = Channel.objects.filter(slug=channel_slug).values("pk")
                shipping_zone_channels = ShippingZoneChannel.objects.filter(
                    Exists(channels.filter(pk=OuterRef("channel_id")))
                ).values("shippingzone_id")
                warehouse_shipping_zones = warehouse_shipping_zones.filter(
                    Exists(
                        shipping_zone_channels.filter(
                            shippingzone_id=OuterRef("shippingzone_id")
                        )
                    )
                )
        warehouse_shipping_zones_map = defaultdict(list)
        for warehouse_shipping_zone in warehouse_shipping_zones:
            warehouse_shipping_zones_map[warehouse_shipping_zone.warehouse_id].append(
                warehouse_shipping_zone.shippingzone_id
            )
        if additional_warehouse_filter:
            stocks = stocks.filter(warehouse_id__in=warehouse_shipping_zones_map.keys())
        stocks = stocks.annotate_available_quantity()

        # A single country code (or a missing country code) can return results from
        # multiple shipping zones. We want to combine all quantities within a single
        # zone and then find out which zone contains the highest total.
        quantity_by_shipping_zone_by_product_variant: DefaultDict[
            int, DefaultDict[int, int]
        ] = defaultdict(lambda: defaultdict(int))
        for stock in stocks:
            quantity = stock.available_quantity
            variant_id = stock.product_variant_id
            warehouse_id = stock.warehouse_id
            shipping_zone_ids = warehouse_shipping_zones_map[warehouse_id]
            for shipping_zone_id in shipping_zone_ids:
                quantity_by_shipping_zone_by_product_variant[variant_id][
                    shipping_zone_id
                ] += quantity

        quantity_map: DefaultDict[int, int] = defaultdict(int)
        for (
            variant_id,
            quantity_by_shipping_zone,
        ) in quantity_by_shipping_zone_by_product_variant.items():
            quantity_values = quantity_by_shipping_zone.values()
            if country_code:
                # When country code is known, return the sum of quantities from all
                # shipping zones supporting given country.
                quantity_map[variant_id] = sum(quantity_values)
            else:
                # When country code is unknown, return the highest known quantity.
                quantity_map[variant_id] = max(quantity_values)

        # Return the quantities after capping them at the maximum quantity allowed in
        # checkout. This prevent users from tracking the store's precise stock levels.
        return [
            (
                variant_id,
                min(quantity_map[variant_id], settings.MAX_CHECKOUT_LINE_QUANTITY),
            )
            for variant_id in variant_ids
        ]


class StocksWithAvailableQuantityByProductVariantIdCountryCodeAndChannelLoader(
    DataLoader[VariantIdCountryCodeChannelSlug, Iterable[Stock]]
):
    """Return stocks with available quantity based on variant ID, country code, channel.

    For each country code, for each shipping zone supporting that country and channel,
    return stocks with maximum available quantity.
    """

    context_key = "stocks_with_available_quantity_by_productvariant_country_and_channel"

    def batch_load(self, keys):
        # Split the list of keys by country first. A typical query will only touch
        # a handful of unique countries but may access thousands of product variants
        # so it's cheaper to execute one query per country.
        variants_by_country_and_channel: DefaultDict[
            CountryCode, List[int]
        ] = defaultdict(list)
        for variant_id, country_code, channel_slug in keys:
            variants_by_country_and_channel[(country_code, channel_slug)].append(
                variant_id
            )

        # For each country code execute a single query for all product variants.
        stocks_by_variant_and_country: DefaultDict[
            VariantIdCountryCodeChannelSlug, Iterable[Stock]
        ] = defaultdict(list)
        for key, variant_ids in variants_by_country_and_channel.items():
            country_code, channel_slug = key
            variant_ids_stocks = self.batch_load_stocks_by_country(
                country_code, channel_slug, variant_ids
            )
            for variant_id, stocks in variant_ids_stocks:
                stocks_by_variant_and_country[
                    (variant_id, country_code, channel_slug)
                ].extend(stocks)

        return [stocks_by_variant_and_country[key] for key in keys]

    def batch_load_stocks_by_country(
        self,
        country_code: Optional[CountryCode],
        channel_slug: Optional[str],
        variant_ids: Iterable[int],
    ) -> Iterable[Tuple[int, List[Stock]]]:
        stocks = Stock.objects.filter(product_variant_id__in=variant_ids)
        if country_code:
            stocks = stocks.filter(
                warehouse__shipping_zones__countries__contains=country_code
            )
        if channel_slug:
            stocks = stocks.filter(
                warehouse__shipping_zones__channels__slug=channel_slug
            )
        stocks = stocks.annotate_available_quantity()

        stocks_by_variant_id_map: DefaultDict[int, List[Stock]] = defaultdict(list)
        for stock in stocks:
            stocks_by_variant_id_map[stock.product_variant_id].append(stock)

        return [
            (
                variant_id,
                stocks_by_variant_id_map[variant_id],
            )
            for variant_id in variant_ids
        ]


class WarehouseByIdLoader(DataLoader):
    context_key = "warehouse_by_id"

    def batch_load(self, keys):
        warehouses = Warehouse.objects.in_bulk(keys)
        return [warehouses.get(UUID(warehouse_uuid)) for warehouse_uuid in keys]


class WarehouseCountryCodeByChannelLoader(DataLoader):
    """Loads country code of a first available warehouse that is found for a channel."""

    context_key = "warehouse_country_code_by_channel"

    def batch_load(self, keys):
        def with_channels(channels):
            address_id_by_channel_slug = dict()
            for channel in channels:
                first_warehouse = Warehouse.objects.get_first_warehouse_for_channel(
                    channel.id
                )
                if first_warehouse:
                    address_id_by_channel_slug[
                        channel.slug
                    ] = first_warehouse.address_id

            def with_addresses(addresses):
                address_by_id = {address.pk: address for address in addresses}
                country_codes = []
                for key in keys:
                    address_id = address_id_by_channel_slug.get(key)
                    address = address_by_id.get(address_id) if address_id else None
                    if address and address.country:
                        country_code = address.country.code
                    else:
                        # Fallback when warehouse address has no country set. API has
                        # validation to prevent from adding such addresses, so this is
                        # added only to handle an edge-case if a warehouse would be
                        # added with bypassing the API (for instance with a migration).
                        country_code = settings.DEFAULT_COUNTRY
                    country_codes.append(country_code)
                return country_codes

            address_ids = address_id_by_channel_slug.values()
            return (
                AddressByIdLoader(self.context)
                .load_many(address_ids)
                .then(with_addresses)
            )

        return ChannelBySlugLoader(self.context).load_many(keys).then(with_channels)
