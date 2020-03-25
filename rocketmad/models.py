#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gc
import itertools
import logging
import os
import sys
import time

from datetime import datetime, timedelta
from flask_sqlalchemy import SQLAlchemy
from functools import reduce
from peewee import (Check, SQL, SmallIntegerField, IntegerField, CharField,
                    DoubleField, BooleanField, DateTimeField, fn, FloatField,
                    TextField, BigIntegerField, JOIN, OperationalError,
                    __exception_wrapper__, Model, DatabaseProxy)
from playhouse.flask_utils import FlaskDB
from playhouse.migrate import migrate, MySQLMigrator
from playhouse.pool import PooledMySQLDatabase
from sqlalchemy import func, Index, text
from sqlalchemy.dialects.mysql import BIGINT, DOUBLE, LONGTEXT, TINYINT
from sqlalchemy.orm import Load, load_only
from sqlalchemy.sql.expression import and_, or_
from timeit import default_timer

from .transform import transform_from_wgs_to_gcj
from .utils import (get_pokemon_name, get_pokemon_types, get_args, cellid,
                    get_utc_timedelta)

log = logging.getLogger(__name__)
args = get_args()

db = SQLAlchemy()
db_schema_version = 0


class Pokemon(db.Model):
    encounter_id = db.Column(BIGINT(unsigned=True), primary_key=True)
    spawnpoint_id = db.Column(BIGINT(unsigned=True), nullable=False)
    pokemon_id = db.Column(db.SmallInteger, nullable=False)
    latitude = db.Column(DOUBLE(asdecimal=False), nullable=False)
    longitude = db.Column(DOUBLE(asdecimal=False), nullable=False)
    disappear_time = db.Column(db.DateTime, nullable=False)
    individual_attack = db.Column(db.SmallInteger)
    individual_defense = db.Column(db.SmallInteger)
    individual_stamina = db.Column(db.SmallInteger)
    move_1 = db.Column(db.SmallInteger)
    move_2 = db.Column(db.SmallInteger)
    cp = db.Column(db.SmallInteger)
    cp_multiplier = db.Column(db.Float)
    weight = db.Column(db.Float)
    height = db.Column(db.Float)
    gender = db.Column(db.SmallInteger)
    form = db.Column(db.SmallInteger)
    costume = db.Column(db.SmallInteger)
    catch_prob_1 = db.Column(DOUBLE)
    catch_prob_2 = db.Column(DOUBLE)
    catch_prob_3 = db.Column(DOUBLE)
    rating_attack = db.Column(
        db.String(length=2, collation='utf8mb4_unicode_ci')
    )
    rating_defense = db.Column(
        db.String(length=2, collation='utf8mb4_unicode_ci')
    )
    weather_boosted_condition = db.Column(db.SmallInteger)
    last_modified = db.Column(db.DateTime)

    __table_args__ = (
        Index('pokemon_spawnpoint_id', 'spawnpoint_id'),
        Index('pokemon_pokemon_id', 'pokemon_id'),
        Index('pokemon_last_modified', 'last_modified'),
        Index('pokemon_latitude_longitude', 'latitude', 'longitude'),
        Index('pokemon_disappear_time_pokemon_id', 'disappear_time',
              'pokemon_id'),
    )

    @staticmethod
    def get_active(swLat, swLng, neLat, neLng, oSwLat=None, oSwLng=None,
                   oNeLat=None, oNeLng=None, timestamp=0, eids=None, ids=None,
                   verified_despawn_time=False):
        columns = [
            Pokemon.encounter_id, Pokemon.pokemon_id, Pokemon.latitude,
            Pokemon.longitude, Pokemon.disappear_time,
            Pokemon.individual_attack, Pokemon.individual_defense,
            Pokemon.individual_stamina, Pokemon.move_1, Pokemon.move_2,
            Pokemon.cp, Pokemon.cp_multiplier, Pokemon.weight, Pokemon.height,
            Pokemon.gender, Pokemon.form, Pokemon.costume,
            Pokemon.weather_boosted_condition, Pokemon.last_modified
        ]

        if verified_despawn_time:
            columns.append(
                TrsSpawn.calc_endminsec.label('verified_disappear_time')
            )
            query = (
                db.session.query(*columns)
                .outerjoin(
                    TrsSpawn, Pokemon.spawnpoint_id == TrsSpawn.spawnpoint
                )
            )
        else:
            query = db.session.query(*columns)

        if eids:
            query = query.filter(Pokemon.pokemon_id.notin_(eids))
        elif ids:
            query = query.filter(Pokemon.pokemon_id.in_(ids))

        if not (swLat and swLng and neLat and neLng):
            query = query.filter(Pokemon.disappear_time > datetime.utcnow())
        elif timestamp > 0:
            # If timestamp is known only load modified Pokémon.
            query = query.filter(
                Pokemon.last_modified > datetime.utcfromtimestamp(
                    timestamp / 1000),
                Pokemon.disappear_time > datetime.utcnow(),
                Pokemon.latitude >= swLat,
                Pokemon.longitude >= swLng,
                Pokemon.latitude <= neLat,
                Pokemon.longitude <= neLng
            )
        elif oSwLat and oSwLng and oNeLat and oNeLng:
            # Send Pokémon in view but exclude those within old boundaries.
            # Only send newly uncovered Pokemon.
            query = query.filter(
                Pokemon.disappear_time > datetime.utcnow(),
                Pokemon.latitude >= swLat,
                Pokemon.longitude >= swLng,
                Pokemon.latitude <= neLat,
                Pokemon.longitude <= neLng,
                ~and_(
                    Pokemon.latitude >= oSwLat,
                    Pokemon.longitude >= oSwLng,
                    Pokemon.latitude <= oNeLat,
                    Pokemon.longitude <= oNeLng
                )
            )
        else:
            query = query.filter(
                Pokemon.disappear_time > datetime.utcnow(),
                Pokemon.latitude >= swLat,
                Pokemon.longitude >= swLng,
                Pokemon.latitude <= neLat,
                Pokemon.longitude <= neLng
            )

        result = query.all()

        return [pokemon._asdict() for pokemon in result]

    # Get all Pokémon spawn counts based on the last x hours.
    # More efficient than get_seen(): we don't do any unnecessary mojo.
    # Returns a dict:
    # { 'pokemon': [ {'pokemon_id': '', 'count': 1} ], 'total': 1 }.
    @staticmethod
    def get_spawn_counts(hours=0):
        query = (
            db.session.query(
                Pokemon.pokemon_id,
                func.count(Pokemon.pokemon_id).label('count')
            )
            .group_by(Pokemon.pokemon_id)
        )
        # Allow 0 to query everything.
        if hours > 0:
            hours = datetime.utcnow() - timedelta(hours=hours)
            query = query.filter(Pokemon.disappear_time > hours)

        result = query.all()

        counts = []
        total = 0
        for c in result:
            counts.append(c._asdict())
            total += c[1]

        return {'pokemon': counts, 'total': total}

    @staticmethod
    def get_seen(timediff=0):
        query = (
            db.session.query(
                Pokemon.pokemon_id, Pokemon.form,
                func.count(Pokemon.pokemon_id).label('count'),
                func.max(Pokemon.disappear_time).label('disappear_time')
            )
            .group_by(Pokemon.pokemon_id, Pokemon.form)
        )
        if timediff > 0:
            timediff = datetime.utcnow() - timedelta(hours=timediff)
            query = query.filter(Pokemon.disappear_time > timediff)

        result = query.all()

        pokemon = []
        total = 0
        for p in result:
            pokemon.append(p._asdict())
            total += p[2]

        return {'pokemon': pokemon, 'total': total}

    @staticmethod
    def get_appearances(pokemon_id, form_id=None, timediff=0):
        '''
        :param pokemon_id: id of Pokémon that we need appearances for
        :param form_id: id of form that we need appearances for
        :param timediff: limiting period of the selection
        :return: list of Pokémon appearances over a selected period
        '''
        query = (
            db.session.query(
                Pokemon.latitude, Pokemon.longitude, Pokemon.pokemon_id,
                Pokemon.form, Pokemon.spawnpoint_id,
                func.count(Pokemon.pokemon_id).label('count')
            )
            .filter(Pokemon.pokemon_id == pokemon_id)
            .group_by(
                Pokemon.latitude, Pokemon.longitude, Pokemon.pokemon_id,
                Pokemon.form, Pokemon.spawnpoint_id
            )
        )
        if form_id is not None:
            query = query.filter(Pokemon.form == form_id)
        if timediff > 0:
            timediff = datetime.utcnow() - timedelta(hours=timediff)
            query = query.filter(Pokemon.disappear_time > timediff)

        result = query.all()

        return [a._asdict() for a in result]

    @staticmethod
    def get_appearances_times_by_spawnpoint(pokemon_id, spawnpoint_id,
                                            form_id=None, timediff=0):
        '''
        :param pokemon_id: id of Pokemon that we need appearances times for.
        :param spawnpoint_id: spawnpoint id we need appearances times for.
        :param timediff: limiting period of the selection.
        :return: list of time appearances over a selected period.
        '''
        query = (
            db.session.query(Pokemon.disappear_time)
            .filter(
                Pokemon.pokemon_id == pokemon_id,
                Pokemon.spawnpoint_id == spawnpoint_id
            )
            .order_by(Pokemon.disappear_time)
        )
        if form_id is not None:
            query = query.filter_by(form=form_id)
        if timediff:
            timediff = datetime.utcnow() - timedelta(hours=timediff)
            query = query.filter(Pokemon.disappear_time > timediff)

        result = query.all()

        return [a[0] for a in result]


class Gym(db.Model):
    gym_id = db.Column(
        db.String(length=50, collation='utf8mb4_unicode_ci'), primary_key=True)
    team_id = db.Column(db.SmallInteger, default=0, nullable=False)
    guard_pokemon_id = db.Column(db.SmallInteger, default=0, nullable=False)
    slots_available = db.Column(db.SmallInteger, default=6, nullable=False)
    enabled = db.Column(TINYINT, default=1, nullable=False)
    latitude = db.Column(DOUBLE(asdecimal=False), nullable=False)
    longitude = db.Column(DOUBLE(asdecimal=False), nullable=False)
    total_cp = db.Column(db.SmallInteger, default=0, nullable=False)
    is_in_battle = db.Column(TINYINT, default=0, nullable=False)
    gender = db.Column(db.SmallInteger)
    form = db.Column(db.SmallInteger)
    costume = db.Column(db.SmallInteger)
    weather_boosted_condition = db.Column(db.SmallInteger)
    shiny = db.Column(TINYINT)
    last_modified = db.Column(
        db.DateTime, default=datetime.utcnow(), nullable=False
    )
    last_scanned = db.Column(
        db.DateTime, default=datetime.utcnow(), nullable=False
    )
    is_ex_raid_eligible = db.Column(TINYINT, default=0, nullable=False)

    gym_details = db.relationship(
        'GymDetails', uselist=False, backref='gym', lazy='joined',
        cascade='delete')

    __table_args__ = (
        Index('gym_last_modified', 'last_modified'),
        Index('gym_last_scanned', 'last_scanned'),
        Index('gym_latitude_longitude', 'latitude', 'longitude'),
    )

    @staticmethod
    def get_gyms(swLat, swLng, neLat, neLng, oSwLat=None, oSwLng=None,
                 oNeLat=None, oNeLng=None, timestamp=0, raids=True):
        if raids:
            query = (
                db.session.query(Gym, Raid)
                .outerjoin(
                    Raid,
                    and_(
                        Gym.gym_id == Raid.gym_id,
                        Raid.end > datetime.utcnow()
                    )
                )
            )
        else:
            query = Gym.query

        if not (swLat and swLng and neLat and neLng):
            pass
        elif timestamp > 0:
            # If timestamp is known only send last scanned Gyms.
            query = query.filter(
                Gym.last_scanned > datetime.utcfromtimestamp(timestamp / 1000),
                Gym.latitude >= swLat,
                Gym.longitude >= swLng,
                Gym.latitude <= neLat,
                Gym.longitude <= neLng
            )
        elif oSwLat and oSwLng and oNeLat and oNeLng:
            # Send gyms in view but exclude those within old boundaries.
            # Only send newly uncovered gyms.
            query = query.filter(
                Gym.latitude >= swLat,
                Gym.longitude >= swLng,
                Gym.latitude <= neLat,
                Gym.longitude <= neLng,
                ~and_(
                    Gym.latitude >= oSwLat,
                    Gym.longitude >= oSwLng,
                    Gym.latitude <= oNeLat,
                    Gym.longitude <= oNeLng
                )
            )
        else:
            query = query.filter(
                Gym.latitude >= swLat,
                Gym.longitude >= swLng,
                Gym.latitude <= neLat,
                Gym.longitude <= neLng
            )

        result = query.all()

        gyms = {}
        for r in result:
            if raids:
                gym = r[0]
                raid = r[1]
            else:
                gym = r
                raid = None
            gym_dict = orm_to_dict(gym)
            del gym_dict['gym_details']
            gym_dict['name'] = gym.gym_details.name
            gym_dict['url'] = gym.gym_details.url
            if raid is not None:
                gym_dict['raid'] = orm_to_dict(raid)
            else:
                gym_dict['raid'] = None
            gyms[gym.gym_id] = gym_dict

        return gyms


class GymDetails(db.Model):
    __tablename__ = 'gymdetails'

    gym_id = db.Column(
        db.String(length=50, collation='utf8mb4_unicode_ci'),
        db.ForeignKey('gym.gym_id', name='fk_gd_gym_id'),
        primary_key=True
    )
    name = db.Column(
        db.String(length=191, collation='utf8mb4_unicode_ci'), nullable=False
    )
    description = db.Column(LONGTEXT(collation='utf8mb4_unicode_ci'))
    url = db.Column(
        db.String(length=191, collation='utf8mb4_unicode_ci'), nullable=False
    )
    last_scanned = db.Column(
        db.DateTime, default=datetime.utcnow(), nullable=False
    )


class Raid(db.Model):
    gym_id = db.Column(
        db.String(length=50, collation='utf8mb4_unicode_ci'), primary_key=True
    )
    level = db.Column(db.Integer, nullable=False)
    spawn = db.Column(db.DateTime, nullable=False)
    start = db.Column(db.DateTime, nullable=False)
    end = db.Column(db.DateTime, nullable=False)
    pokemon_id = db.Column(db.SmallInteger)
    cp = db.Column(db.Integer)
    move_1 = db.Column(db.SmallInteger)
    move_2 = db.Column(db.SmallInteger)
    last_scanned = db.Column(db.DateTime, nullable=False)
    form = db.Column(db.SmallInteger)
    is_exclusive = db.Column(TINYINT)
    gender = db.Column(TINYINT)
    costume = db.Column(TINYINT)

    __table_args__ = (
        Index('raid_level', 'level'),
        Index('raid_spawn', 'spawn'),
        Index('raid_start', 'start'),
        Index('raid_end', 'end'),
        Index('raid_last_scanned', 'last_scanned'),
    )


class Pokestop(db.Model):
    pokestop_id = db.Column(
        db.String(length=50, collation='utf8mb4_unicode_ci'), primary_key=True
    )
    enabled = db.Column(TINYINT, default=1, nullable=False)
    latitude = db.Column(DOUBLE(asdecimal=False), nullable=False)
    longitude = db.Column(DOUBLE(asdecimal=False), nullable=False)
    last_modified = db.Column(db.DateTime, default=datetime.utcnow())
    lure_expiration = db.Column(db.DateTime)
    active_fort_modifier = db.Column(db.SmallInteger)
    last_updated = db.Column(db.DateTime)
    name = db.Column(db.String(length=250, collation='utf8mb4_unicode_ci'))
    image = db.Column(db.String(length=255, collation='utf8mb4_unicode_ci'))
    incident_start = db.Column(db.DateTime)
    incident_expiration = db.Column(db.DateTime)
    incident_grunt_type = db.Column(db.SmallInteger)

    __table_args__ = (
        Index('pokestop_last_modified', 'last_modified'),
        Index('pokestop_lure_expiration', 'lure_expiration'),
        Index('pokestop_active_fort_modifier', 'active_fort_modifier'),
        Index('pokestop_last_updated', 'last_updated'),
        Index('pokestop_latitude_longitude', 'latitude', 'longitude'),
    )

    @staticmethod
    def get_pokestops(swLat, swLng, neLat, neLng, oSwLat=None, oSwLng=None,
                      oNeLat=None, oNeLng=None, timestamp=0,
                      eventless_stops=True, quests=True, invasions=True,
                      lures=True):
        columns = [
            'pokestop_id', 'name', 'image', 'latitude', 'longitude',
            'last_updated', 'incident_grunt_type', 'incident_expiration',
            'active_fort_modifier', 'lure_expiration'
        ]

        if quests:
            quest_columns = [
                'GUID', 'quest_timestamp', 'quest_task', 'quest_type',
                'quest_stardust', 'quest_pokemon_id', 'quest_pokemon_form_id',
                'quest_pokemon_costume_id', 'quest_reward_type',
                'quest_item_id', 'quest_item_amount'
            ]
            today = datetime.today().replace(
                hour=0, minute=0, second=0, microsecond=0)  # Local time.
            today_timestamp = datetime.timestamp(today)
            query = (
                db.session.query(Pokestop, TrsQuest)
                .outerjoin(
                    TrsQuest,
                    and_(
                        Pokestop.pokestop_id == TrsQuest.GUID,
                        TrsQuest.quest_timestamp >= today_timestamp
                    )
                )
                .options(
                    Load(Pokestop).load_only(*columns),
                    Load(TrsQuest).load_only(*quest_columns)
                )
            )
        else:
            query = Pokestop.query.options(load_only(*columns))

        if not eventless_stops:
            terms = []
            if quests:
                terms.append(TrsQuest.GUID != None)
            if invasions:
                terms.append(Pokestop.incident_expiration > datetime.utcnow())
            if lures:
                terms.append(Pokestop.lure_expiration > datetime.utcnow())
            query = query.filter(or_(*terms))

        if not (swLat and swLng and neLat and neLng):
            pass
        elif timestamp > 0:
            # If timestamp is known only send last scanned PokéStops.
            query = query.filter(
                Pokestop.last_updated > datetime.utcfromtimestamp(
                    timestamp / 1000),
                Pokestop.latitude >= swLat,
                Pokestop.longitude >= swLng,
                Pokestop.latitude <= neLat,
                Pokestop.longitude <= neLng
            )
        elif oSwLat and oSwLng and oNeLat and oNeLng:
            # Send gyms in view but exclude those within old boundaries.
            # Only send newly uncovered gyms.
            query = query.filter(
                Pokestop.latitude >= swLat,
                Pokestop.longitude >= swLng,
                Pokestop.latitude <= neLat,
                Pokestop.longitude <= neLng,
                ~and_(
                    Pokestop.latitude >= oSwLat,
                    Pokestop.longitude >= oSwLng,
                    Pokestop.latitude <= oNeLat,
                    Pokestop.longitude <= oNeLng
                )
            )
        else:
            query = query.filter(
                Pokestop.latitude >= swLat,
                Pokestop.longitude >= swLng,
                Pokestop.latitude <= neLat,
                Pokestop.longitude <= neLng
            )

        result = query.all()

        now = datetime.utcnow()
        pokestops = {}
        for r in result:
            pokestop_orm = r[0] if quests else r
            quest_orm = r[1] if quests else None
            pokestop = orm_to_dict(pokestop_orm)
            if quest_orm is not None:
                quest = {}
                quest['pokestop_id'] = quest_orm.GUID
                quest['timestamp'] = quest_orm.quest_timestamp
                quest['task'] = quest_orm.quest_task
                quest['type'] = quest_orm.quest_type
                quest['stardust'] = quest_orm.quest_stardust
                quest['pokemon_id'] = quest_orm.quest_pokemon_id
                quest['form_id'] = quest_orm.quest_pokemon_form_id
                quest['costume_id'] = quest_orm.quest_pokemon_costume_id
                quest['reward_type'] = quest_orm.quest_reward_type
                quest['item_id'] = quest_orm.quest_item_id
                quest['item_amount'] = quest_orm.quest_item_amount
                pokestop['quest'] = quest
            else:
                pokestop['quest'] = None
            if (pokestop['incident_expiration'] is not None and
                    (pokestop['incident_expiration'] < now or not invasions)):
                pokestop['incident_grunt_type'] = None
                pokestop['incident_expiration'] = None
            if (pokestop['lure_expiration'] is not None and
                    (pokestop['lure_expiration'] < now or not lures)):
                pokestop['active_fort_modifier'] = None
                pokestop['lure_expiration'] = None
            pokestops[pokestop['pokestop_id']] = pokestop

        return pokestops


class TrsQuest(db.Model):
    GUID = db.Column(
        db.String(length=50, collation='utf8mb4_unicode_ci'), primary_key=True
    )
    quest_type = db.Column(TINYINT, nullable=False)
    quest_timestamp = db.Column(db.Integer, nullable=False)
    quest_stardust = db.Column(db.SmallInteger, nullable=False)
    quest_pokemon_id = db.Column(db.SmallInteger, nullable=False)
    quest_reward_type = db.Column(db.SmallInteger, nullable=False)
    quest_item_id = db.Column(db.SmallInteger, nullable=False)
    quest_item_amount = db.Column(TINYINT, nullable=False)
    quest_target = db.Column(TINYINT, nullable=False)
    quest_condition = db.Column(
        db.String(length=500, collation='utf8mb4_unicode_ci')
    )
    quest_reward = db.Column(
        db.String(length=1000, collation='utf8mb4_unicode_ci')
    )
    quest_template = db.Column(
        db.String(length=100, collation='utf8mb4_unicode_ci')
    )
    quest_task = db.Column(
        db.String(length=150, collation='utf8mb4_unicode_ci')
    )
    quest_pokemon_form_id = db.Column(
        db.SmallInteger, default=0, nullable=False
    )
    quest_pokemon_costume_id = db.Column(
        db.SmallInteger, default=0, nullable=False
    )

    __table_args__ = (
        Index('quest_type', 'quest_type'),
    )


class Weather(db.Model):
    s2_cell_id = db.Column(
        db.String(length=50, collation='utf8mb4_unicode_ci'), primary_key=True
    )
    latitude = db.Column(DOUBLE(asdecimal=False), nullable=False)
    longitude = db.Column(DOUBLE(asdecimal=False), nullable=False)
    cloud_level = db.Column(db.SmallInteger)
    rain_level = db.Column(db.SmallInteger)
    wind_level = db.Column(db.SmallInteger)
    snow_level = db.Column(db.SmallInteger)
    fog_level = db.Column(db.SmallInteger)
    wind_direction = db.Column(db.SmallInteger)
    gameplay_weather = db.Column(db.SmallInteger)
    severity = db.Column(db.SmallInteger)
    warn_weather = db.Column(db.SmallInteger)
    world_time = db.Column(db.SmallInteger)
    last_updated = db.Column(db.DateTime)

    __table_args__ = (
        Index('weather_cloud_level', 'cloud_level'),
        Index('weather_rain_level', 'rain_level'),
        Index('weather_wind_level', 'wind_level'),
        Index('weather_snow_level', 'snow_level'),
        Index('weather_fog_level', 'fog_level'),
        Index('weather_wind_direction', 'wind_direction'),
        Index('weather_gameplay_weather', 'gameplay_weather'),
        Index('weather_severity', 'severity'),
        Index('weather_warn_weather', 'warn_weather'),
        Index('weather_world_time', 'world_time'),
        Index('weather_last_updated', 'last_updated'),
    )

    @staticmethod
    def get_weather(swLat, swLng, neLat, neLng, oSwLat=None, oSwLng=None,
                    oNeLat=None, oNeLng=None, timestamp=0):
        # We can filter by the center of a cell,
        # this deltas can expand the viewport bounds
        # So cells with center outside the viewport,
        # but close to it can be rendered
        # otherwise edges of cells that intersects
        # with viewport won't be rendered
        lat_delta = 0.15
        lng_delta = 0.4

        query = db.session.query(
            Weather.s2_cell_id, Weather.latitude, Weather.longitude,
            Weather.gameplay_weather, Weather.severity, Weather.world_time,
            Weather.last_updated
        )

        if not (swLat and swLng and neLat and neLng):
            pass
        elif timestamp > 0:
            # If timestamp is known only send last scanned weather.
            query = query.filter(
                Weather.last_updated > datetime.utcfromtimestamp(
                    timestamp / 1000),
                Weather.latitude >= float(swLat) - lat_delta,
                Weather.longitude >= float(swLng) - lng_delta,
                Weather.latitude <= float(neLat) + lat_delta,
                Weather.longitude <= float(neLng) + lng_delta
            )
        elif oSwLat and oSwLng and oNeLat and oNeLng:
            # Send weather in view but exclude those within old boundaries.
            # Only send newly uncovered weather.
            query = query.filter(
                Weather.latitude >= float(swLat) - lat_delta,
                Weather.longitude >= float(swLng) - lng_delta,
                Weather.latitude <= float(neLat) + lat_delta,
                Weather.longitude <= float(neLng) + lng_delta,
                ~and_(
                    Weather.latitude >= float(oSwLat) - lat_delta,
                    Weather.longitude >= float(oSwLng) - lng_delta,
                    Weather.latitude <= float(oNeLat) + lat_delta,
                    Weather.longitude <= float(oNeLng) + lng_delta
                )
            )
        else:
            query = query.filter(
                Weather.latitude >= float(swLat) - lat_delta,
                Weather.longitude >= float(swLng) - lng_delta,
                Weather.latitude <= float(neLat) + lat_delta,
                Weather.longitude <= float(neLng) + lng_delta
            )

        result = query.all()

        return [w._asdict() for w in result]


class TrsSpawn(db.Model):
    spawnpoint = db.Column(
        db.String(length=16, collation='utf8mb4_unicode_ci'), primary_key=True
    )
    latitude = db.Column(DOUBLE(asdecimal=False), nullable=False)
    longitude = db.Column(DOUBLE(asdecimal=False), nullable=False)
    spawndef = db.Column(db.Integer, default=240, nullable=False)
    earliest_unseen = db.Column(db.Integer, nullable=False)
    last_scanned = db.Column(db.DateTime)
    first_detection = db.Column(
        db.DateTime, default=datetime.utcnow(), nullable=False
    )
    last_non_scanned = db.Column(db.DateTime)
    calc_endminsec = db.Column(
        db.String(length=5, collation='utf8mb4_unicode_ci')
    )
    eventid = db.Column(db.Integer, default=1, nullable=False)

    __table_args__ = (
        Index('spawnpoint_2', 'spawnpoint', unique=True),
        Index('spawnpoint', 'spawnpoint')
    )

    @staticmethod
    def get_spawnpoints(swLat, swLng, neLat, neLng, oSwLat=None, oSwLng=None,
                        oNeLat=None, oNeLng=None, timestamp=0):
        query = db.session.query(
            TrsSpawn.latitude, TrsSpawn.longitude,
            TrsSpawn.spawnpoint.label('spawnpoint_id'), TrsSpawn.spawndef,
            TrsSpawn.first_detection, TrsSpawn.last_non_scanned,
            TrsSpawn.last_scanned, TrsSpawn.calc_endminsec.label('end_time')
        )

        if not (swLat and swLng and neLat and neLng):
            pass
        elif timestamp > 0:
            # If timestamp is known only send last scanned spawn points.
            utc_time = datetime.utcfromtimestamp(timestamp / 1000)
            query = query.filter(
                ((TrsSpawn.last_scanned > utc_time) |
                 (TrsSpawn.last_non_scanned > utc_time)),
                TrsSpawn.latitude >= swLat,
                TrsSpawn.longitude >= swLng,
                TrsSpawn.latitude <= neLat,
                TrsSpawn.longitude <= neLng
            )
        elif oSwLat and oSwLng and oNeLat and oNeLng:
            # Send spawn points in view but exclude those within old
            # boundaries. Only send newly uncovered spawn points.
            query = query.filter(
                TrsSpawn.latitude >= swLat,
                TrsSpawn.longitude >= swLng,
                TrsSpawn.latitude <= neLat,
                TrsSpawn.longitude <= neLng,
                ~and_(
                    TrsSpawn.latitude >= oSwLat,
                    TrsSpawn.longitude >= oSwLng,
                    TrsSpawn.latitude <= oNeLat,
                    TrsSpawn.longitude <= oNeLng
                )
            )
        else:
            query = query.filter(
                TrsSpawn.latitude >= swLat,
                TrsSpawn.longitude >= swLng,
                TrsSpawn.latitude <= neLat,
                TrsSpawn.longitude <= neLng
            )

        result = query.all()

        spawnpoints = []
        ts = time.time()
        utc_offset = datetime.fromtimestamp(ts) - datetime.utcfromtimestamp(ts)
        for sp in result:
            sp = sp._asdict()
            if sp['last_non_scanned'] is not None:
                sp['last_non_scanned'] = sp['last_non_scanned'] - utc_offset
            if sp['end_time'] is not None:
                if sp['last_scanned'] is not None:
                    sp['last_scanned'] = sp['last_scanned'] - utc_offset
                end_time_split = sp['end_time'].split(':')
                end_time_seconds = int(end_time_split[1])
                end_time_minutes = int(end_time_split[0])
                despawn_time = datetime.today().replace(
                    minute=end_time_minutes, second=end_time_seconds,
                    microsecond=0
                )
                if despawn_time <= datetime.today():
                    despawn_time += timedelta(hours=1)
                sp['despawn_time'] = despawn_time - utc_offset
                if sp['spawndef'] == 15:
                    sp['spawn_time'] = sp['despawn_time'] - timedelta(hours=1)
                else:
                    sp['spawn_time'] = (sp['despawn_time'] -
                                        timedelta(minutes=30))
                del sp['end_time']
            spawnpoints.append(sp)

        return spawnpoints


class ScannedLocation(db.Model):
    __tablename__ = 'scannedlocation'

    cellid = db.Column(BIGINT(unsigned=True), primary_key=True)
    latitude = db.Column(DOUBLE(asdecimal=False), nullable=False)
    longitude = db.Column(DOUBLE(asdecimal=False), nullable=False)
    last_modified = db.Column(db.DateTime)

    __table_args__ = (
        Index('scannedlocation_last_modified', 'last_modified'),
        Index('scannedlocation_latitude_longitude', 'latitude', 'longitude')
    )

    @staticmethod
    def get_recent(swLat, swLng, neLat, neLng, oSwLat=None, oSwLng=None,
                   oNeLat=None, oNeLng=None, timestamp=0):
        query = db.session.query(
            ScannedLocation.cellid, ScannedLocation.latitude,
            ScannedLocation.longitude, ScannedLocation.last_modified
        )

        active_time = datetime.utcnow() - timedelta(minutes=15)

        if not (swLat and swLng and neLat and neLng):
            query = query.filter(ScannedLocation.last_modified >= active_time)
        elif timestamp > 0:
            query = query.filter(
                ScannedLocation.last_modified >= datetime.utcfromtimestamp(
                    timestamp / 1000),
                ScannedLocation.latitude >= swLat,
                ScannedLocation.longitude >= swLng,
                ScannedLocation.latitude <= neLat,
                ScannedLocation.longitude <= neLng
            )
        elif oSwLat and oSwLng and oNeLat and oNeLng:
            # Send scanned locations in view but exclude those within old
            # boundaries. Only send newly uncovered scanned locations.
            query = query.filter(
                ScannedLocation.last_modified >= active_time,
                ScannedLocation.latitude >= swLat,
                ScannedLocation.longitude >= swLng,
                ScannedLocation.latitude <= neLat,
                ScannedLocation.longitude <= neLng,
                ~and_(
                    ScannedLocation.latitude >= oSwLat,
                    ScannedLocation.longitude >= oSwLng,
                    ScannedLocation.latitude <= oNeLat,
                    ScannedLocation.longitude <= oNeLng
                )
            )
        else:
            query = query.filter(
                ScannedLocation.last_modified >= active_time,
                ScannedLocation.latitude >= swLat,
                ScannedLocation.longitude >= swLng,
                ScannedLocation.latitude <= neLat,
                ScannedLocation.longitude <= neLng
            )

        result = query.all()

        return [loc._asdict() for loc in result]


class RmVersion(db.Model):
    __tablename__ = 'rmversion'

    key = db.Column(
        db.String(length=16, collation='utf8mb4_unicode_ci'), primary_key=True
    )
    val = db.Column(db.SmallInteger)


def orm_to_dict(orm_result):
    if isinstance(orm_result, list):
        result = []
        for r in orm_result:
            d = dict(r.__dict__)
            del d['_sa_instance_state']
            result.append(d)
    else:
        result = dict(orm_result.__dict__)
        del result['_sa_instance_state']
    return result


def table_exists(table_model):
    return db.engine.has_table(table_model.__tablename__)


def create_table(table_model):
    if not table_exists(table_model):
        table_model.__table__.create(db.engine)


def create_rm_tables():
    tables = []
    for table in tables:
        if not table_exists(table):
            log.info('Creating table: %s', table.__tablename__)
            table.__table__.create(db.engine)
        else:
            log.debug('Skipping table %s, it already exists.',
                      table.__tablename__)


def drop_table(table_model=None, table_name=None):
    if table_name:
        query = 'DROP TABLE IF EXISTS {}'.format(table_name)
        db.session.execute(text(query))
    elif table_model and table_exists(table_model):
        table_model.__table__.drop(db.engine)


def drop_rm_tables():
    tables = [RmVersion]
    for table in tables:
        if table_exists(table):
            log.info('Dropping table: %s', table.__tablename__)
            table.__table__.drop(db.engine)
        else:
            log.debug('Skipping table %s, it doesn\'t exist.',
                      table.__tablename__)


def add_column(table_model, column):
    column_name = column.compile(dialect=db.engine.dialect)
    column_type = column.type.compile(db.engine.dialect)
    db.session.execute('ALTER TABLE %s ADD COLUMN %s %s'.format(
        table_name, column_name, column_type))


def verify_database_schema():
    if not table_exists(RmVersion):
        RmVersion.__table__.create(db.session.bind)
        db_ver = RmVersion(key='schema_version', val=-1)
        db.session.add(db_ver)
        db.session.commit()
        database_migrate(-1)
    else:
        db_ver = RmVersion.query.get('schema_version').val

        if db_ver < db_schema_version:
            if not database_migrate(db_ver):
                log.error('Error migrating database.')
                sys.exit(1)
        elif db_ver > db_schema_version:
            log.error('Your database version (%i) appears to be newer than '
                      'the code supports (%i).', db_ver, db_schema_version)
            log.error('Please upgrade your code base or drop all tables in '
                      'your database.')
            sys.exit(1)


def database_migrate(old_ver):
    log.info('Detected database version %i, updating to %i...',
             old_ver, db_schema_version)

    # Update database schema version.
    db_ver = RmVersion.query.get('schema_version')
    db_ver.val = db_schema_version
    db.session.commit()

    # Perform migrations here.
    if old_ver < 1:
        drop_table(table_name='rmversions')

    # Always log that we're done.
    log.info('Schema upgrade complete.')
    return True


def clean_db_loop(app):
    # Run regular database cleanup once every minute.
    regular_cleanup_secs = 60
    # Run full database cleanup once every 10 minutes.
    full_cleanup_timer = default_timer()
    full_cleanup_secs = 600
    with app.app_context():
        db_cleanup_regular()
    return
    while True:
        try:
            db_cleanup_regular()

            # Check if it's time to run full database cleanup.
            now = default_timer()
            if now - full_cleanup_timer > full_cleanup_secs:
                # Remove old pokemon spawns.
                if args.db_cleanup_pokemon > 0:
                    db_clean_pokemons(args.db_cleanup_pokemon)

                # Remove old gym data.
                if args.db_cleanup_gym > 0:
                    db_clean_gyms(args.db_cleanup_gym)

                # Remove old and extinct spawnpoint data.
                if args.db_cleanup_spawnpoint > 0:
                    db_clean_spawnpoints(args.db_cleanup_spawnpoint)

                # Remove old pokestop and gym locations.
                if args.db_cleanup_forts > 0:
                    db_clean_forts(args.db_cleanup_forts)

                # Clean weather... only changes at full hours anyway...
                with db:
                    query = (Weather
                             .delete()
                             .where(Weather.last_updated <
                                    datetime.utcnow() - timedelta(minutes=15)))
                    query.execute()

                log.info('Full database cleanup completed.')
                full_cleanup_timer = now

            time.sleep(regular_cleanup_secs)
        except Exception as e:
            log.exception('Database cleanup failed: %s.', e)


def db_cleanup_regular():
    log.debug('Regular database cleanup started.')
    start_timer = default_timer()

    now = datetime.utcnow()
    # Remove active modifier from expired lured PokéStops.
    Pokestop.query.filter(Pokestop.lure_expiration < datetime.utcnow()).update(
        dict(lure_expiration=None, active_fort_modifier=None))
    db.session.commit()

    time_diff = default_timer() - start_timer
    log.debug('Completed regular cleanup in %.6f seconds.', time_diff)


def db_clean_pokemons(age_hours):
    log.debug('Beginning cleanup of old pokemon spawns.')
    start_timer = default_timer()
    pokemon_timeout = datetime.utcnow() - timedelta(hours=age_hours)
    with db:
        query = (Pokemon
                 .delete()
                 .where(Pokemon.disappear_time < pokemon_timeout))
        rows = query.execute()
        log.debug('Deleted %d old Pokemon entries.', rows)

    time_diff = default_timer() - start_timer
    log.debug('Completed cleanup of old pokemon spawns in %.6f seconds.',
              time_diff)


def db_clean_gyms(age_hours, gyms_age_days=30):
    log.debug('Beginning cleanup of old gym data.')
    start_timer = default_timer()

    gym_info_timeout = datetime.utcnow() - timedelta(hours=age_hours)

    with db:
        # Remove old GymDetails entries.
        query = (GymDetails
                 .delete()
                 .where(GymDetails.last_scanned < gym_info_timeout))
        rows = query.execute()
        log.debug('Deleted %d old GymDetails entries.', rows)

        # Remove old Raid entries.
        query = (Raid
                 .delete()
                 .where(Raid.end < gym_info_timeout))
        rows = query.execute()
        log.debug('Deleted %d old Raid entries.', rows)

    time_diff = default_timer() - start_timer
    log.debug('Completed cleanup of old gym data in %.6f seconds.',
              time_diff)


def db_clean_spawnpoints(age_hours):
    log.debug('Beginning cleanup of old spawnpoint data.')
    start_timer = default_timer()
    # Maximum number of variables to include in a single query.
    step = 500

    spawnpoint_timeout = datetime.now() - timedelta(hours=age_hours)

    with db:
        # Select old Trs_Spawn entries.
        query = (Trs_Spawn
                 .select(Trs_Spawn.spawnpoint)
                 .where((Trs_Spawn.last_scanned < spawnpoint_timeout) &
                        (Trs_Spawn.last_non_scanned < spawnpoint_timeout))
                 .dicts())
        old_sp = [(sp['spawnpoint']) for sp in query]

        num_records = len(old_sp)
        log.debug('Found %d old Trs_Spawn entries.', num_records)

        # Remove old and invalid Trs_Spawn entries.
        num_rows = 0
        for i in range(0, num_records, step):
            query = (Trs_Spawn
                     .delete()
                     .where((Trs_Spawn.spawnpoint <<
                             old_sp[i:min(i + step, num_records)])))
            num_rows += query.execute()
        log.debug('Deleted %d old Trs_Spawn entries.', num_rows)

    time_diff = default_timer() - start_timer
    log.debug('Completed cleanup of old spawnpoint data in %.6f seconds.',
              time_diff)


def db_clean_forts(age_hours):
    log.debug('Beginning cleanup of old forts.')
    start_timer = default_timer()

    fort_timeout = datetime.utcnow() - timedelta(hours=age_hours)

    with db:
        # Remove old Gym entries.
        query = (Gym
                 .delete()
                 .where(Gym.last_scanned < fort_timeout))
        rows = query.execute()
        log.debug('Deleted %d old Gym entries.', rows)

        # Remove old Pokestop entries.
        query = (Pokestop
                 .delete()
                 .where(Pokestop.last_updated < fort_timeout))
        rows = query.execute()
        log.debug('Deleted %d old Pokestop entries.', rows)

    time_diff = default_timer() - start_timer
    log.debug('Completed cleanup of old forts in %.6f seconds.',
              time_diff)
