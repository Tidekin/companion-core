// GENERATED CODE - DO NOT MODIFY BY HAND

part of 'database.dart';

// ignore_for_file: type=lint
class $BotRowsTable extends BotRows with TableInfo<$BotRowsTable, BotRow> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $BotRowsTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _botIdMeta = const VerificationMeta('botId');
  @override
  late final GeneratedColumn<String> botId = GeneratedColumn<String>(
    'bot_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _displayNameMeta = const VerificationMeta(
    'displayName',
  );
  @override
  late final GeneratedColumn<String> displayName = GeneratedColumn<String>(
    'display_name',
    aliasedName,
    false,
    additionalChecks: GeneratedColumn.checkTextLength(
      minTextLength: 1,
      maxTextLength: 80,
    ),
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _backendMeta = const VerificationMeta(
    'backend',
  );
  @override
  late final GeneratedColumn<String> backend = GeneratedColumn<String>(
    'backend',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _endpointMeta = const VerificationMeta(
    'endpoint',
  );
  @override
  late final GeneratedColumn<String> endpoint = GeneratedColumn<String>(
    'endpoint',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  @override
  List<GeneratedColumn> get $columns => [botId, displayName, backend, endpoint];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'bot_rows';
  @override
  VerificationContext validateIntegrity(
    Insertable<BotRow> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('bot_id')) {
      context.handle(
        _botIdMeta,
        botId.isAcceptableOrUnknown(data['bot_id']!, _botIdMeta),
      );
    } else if (isInserting) {
      context.missing(_botIdMeta);
    }
    if (data.containsKey('display_name')) {
      context.handle(
        _displayNameMeta,
        displayName.isAcceptableOrUnknown(
          data['display_name']!,
          _displayNameMeta,
        ),
      );
    } else if (isInserting) {
      context.missing(_displayNameMeta);
    }
    if (data.containsKey('backend')) {
      context.handle(
        _backendMeta,
        backend.isAcceptableOrUnknown(data['backend']!, _backendMeta),
      );
    } else if (isInserting) {
      context.missing(_backendMeta);
    }
    if (data.containsKey('endpoint')) {
      context.handle(
        _endpointMeta,
        endpoint.isAcceptableOrUnknown(data['endpoint']!, _endpointMeta),
      );
    } else if (isInserting) {
      context.missing(_endpointMeta);
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {botId};
  @override
  List<Set<GeneratedColumn>> get uniqueKeys => [
    {backend, endpoint},
  ];
  @override
  BotRow map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return BotRow(
      botId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}bot_id'],
      )!,
      displayName: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}display_name'],
      )!,
      backend: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}backend'],
      )!,
      endpoint: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}endpoint'],
      )!,
    );
  }

  @override
  $BotRowsTable createAlias(String alias) {
    return $BotRowsTable(attachedDatabase, alias);
  }
}

class BotRow extends DataClass implements Insertable<BotRow> {
  final String botId;
  final String displayName;
  final String backend;
  final String endpoint;
  const BotRow({
    required this.botId,
    required this.displayName,
    required this.backend,
    required this.endpoint,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['bot_id'] = Variable<String>(botId);
    map['display_name'] = Variable<String>(displayName);
    map['backend'] = Variable<String>(backend);
    map['endpoint'] = Variable<String>(endpoint);
    return map;
  }

  BotRowsCompanion toCompanion(bool nullToAbsent) {
    return BotRowsCompanion(
      botId: Value(botId),
      displayName: Value(displayName),
      backend: Value(backend),
      endpoint: Value(endpoint),
    );
  }

  factory BotRow.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return BotRow(
      botId: serializer.fromJson<String>(json['botId']),
      displayName: serializer.fromJson<String>(json['displayName']),
      backend: serializer.fromJson<String>(json['backend']),
      endpoint: serializer.fromJson<String>(json['endpoint']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'botId': serializer.toJson<String>(botId),
      'displayName': serializer.toJson<String>(displayName),
      'backend': serializer.toJson<String>(backend),
      'endpoint': serializer.toJson<String>(endpoint),
    };
  }

  BotRow copyWith({
    String? botId,
    String? displayName,
    String? backend,
    String? endpoint,
  }) => BotRow(
    botId: botId ?? this.botId,
    displayName: displayName ?? this.displayName,
    backend: backend ?? this.backend,
    endpoint: endpoint ?? this.endpoint,
  );
  BotRow copyWithCompanion(BotRowsCompanion data) {
    return BotRow(
      botId: data.botId.present ? data.botId.value : this.botId,
      displayName: data.displayName.present
          ? data.displayName.value
          : this.displayName,
      backend: data.backend.present ? data.backend.value : this.backend,
      endpoint: data.endpoint.present ? data.endpoint.value : this.endpoint,
    );
  }

  @override
  String toString() {
    return (StringBuffer('BotRow(')
          ..write('botId: $botId, ')
          ..write('displayName: $displayName, ')
          ..write('backend: $backend, ')
          ..write('endpoint: $endpoint')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(botId, displayName, backend, endpoint);
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is BotRow &&
          other.botId == this.botId &&
          other.displayName == this.displayName &&
          other.backend == this.backend &&
          other.endpoint == this.endpoint);
}

class BotRowsCompanion extends UpdateCompanion<BotRow> {
  final Value<String> botId;
  final Value<String> displayName;
  final Value<String> backend;
  final Value<String> endpoint;
  final Value<int> rowid;
  const BotRowsCompanion({
    this.botId = const Value.absent(),
    this.displayName = const Value.absent(),
    this.backend = const Value.absent(),
    this.endpoint = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  BotRowsCompanion.insert({
    required String botId,
    required String displayName,
    required String backend,
    required String endpoint,
    this.rowid = const Value.absent(),
  }) : botId = Value(botId),
       displayName = Value(displayName),
       backend = Value(backend),
       endpoint = Value(endpoint);
  static Insertable<BotRow> custom({
    Expression<String>? botId,
    Expression<String>? displayName,
    Expression<String>? backend,
    Expression<String>? endpoint,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (botId != null) 'bot_id': botId,
      if (displayName != null) 'display_name': displayName,
      if (backend != null) 'backend': backend,
      if (endpoint != null) 'endpoint': endpoint,
      if (rowid != null) 'rowid': rowid,
    });
  }

  BotRowsCompanion copyWith({
    Value<String>? botId,
    Value<String>? displayName,
    Value<String>? backend,
    Value<String>? endpoint,
    Value<int>? rowid,
  }) {
    return BotRowsCompanion(
      botId: botId ?? this.botId,
      displayName: displayName ?? this.displayName,
      backend: backend ?? this.backend,
      endpoint: endpoint ?? this.endpoint,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (botId.present) {
      map['bot_id'] = Variable<String>(botId.value);
    }
    if (displayName.present) {
      map['display_name'] = Variable<String>(displayName.value);
    }
    if (backend.present) {
      map['backend'] = Variable<String>(backend.value);
    }
    if (endpoint.present) {
      map['endpoint'] = Variable<String>(endpoint.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('BotRowsCompanion(')
          ..write('botId: $botId, ')
          ..write('displayName: $displayName, ')
          ..write('backend: $backend, ')
          ..write('endpoint: $endpoint, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $TurnRowsTable extends TurnRows with TableInfo<$TurnRowsTable, TurnRow> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $TurnRowsTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _botIdMeta = const VerificationMeta('botId');
  @override
  late final GeneratedColumn<String> botId = GeneratedColumn<String>(
    'bot_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'REFERENCES bot_rows (bot_id) ON DELETE CASCADE',
    ),
  );
  static const VerificationMeta _eventIdMeta = const VerificationMeta(
    'eventId',
  );
  @override
  late final GeneratedColumn<String> eventId = GeneratedColumn<String>(
    'event_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _sequenceMeta = const VerificationMeta(
    'sequence',
  );
  @override
  late final GeneratedColumn<int> sequence = GeneratedColumn<int>(
    'sequence',
    aliasedName,
    false,
    type: DriftSqlType.int,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _payloadMeta = const VerificationMeta(
    'payload',
  );
  @override
  late final GeneratedColumn<Uint8List> payload = GeneratedColumn<Uint8List>(
    'payload',
    aliasedName,
    false,
    type: DriftSqlType.blob,
    requiredDuringInsert: true,
  );
  @override
  List<GeneratedColumn> get $columns => [botId, eventId, sequence, payload];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'turn_rows';
  @override
  VerificationContext validateIntegrity(
    Insertable<TurnRow> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('bot_id')) {
      context.handle(
        _botIdMeta,
        botId.isAcceptableOrUnknown(data['bot_id']!, _botIdMeta),
      );
    } else if (isInserting) {
      context.missing(_botIdMeta);
    }
    if (data.containsKey('event_id')) {
      context.handle(
        _eventIdMeta,
        eventId.isAcceptableOrUnknown(data['event_id']!, _eventIdMeta),
      );
    } else if (isInserting) {
      context.missing(_eventIdMeta);
    }
    if (data.containsKey('sequence')) {
      context.handle(
        _sequenceMeta,
        sequence.isAcceptableOrUnknown(data['sequence']!, _sequenceMeta),
      );
    } else if (isInserting) {
      context.missing(_sequenceMeta);
    }
    if (data.containsKey('payload')) {
      context.handle(
        _payloadMeta,
        payload.isAcceptableOrUnknown(data['payload']!, _payloadMeta),
      );
    } else if (isInserting) {
      context.missing(_payloadMeta);
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {botId, eventId};
  @override
  TurnRow map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return TurnRow(
      botId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}bot_id'],
      )!,
      eventId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}event_id'],
      )!,
      sequence: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}sequence'],
      )!,
      payload: attachedDatabase.typeMapping.read(
        DriftSqlType.blob,
        data['${effectivePrefix}payload'],
      )!,
    );
  }

  @override
  $TurnRowsTable createAlias(String alias) {
    return $TurnRowsTable(attachedDatabase, alias);
  }
}

class TurnRow extends DataClass implements Insertable<TurnRow> {
  final String botId;
  final String eventId;
  final int sequence;
  final Uint8List payload;
  const TurnRow({
    required this.botId,
    required this.eventId,
    required this.sequence,
    required this.payload,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['bot_id'] = Variable<String>(botId);
    map['event_id'] = Variable<String>(eventId);
    map['sequence'] = Variable<int>(sequence);
    map['payload'] = Variable<Uint8List>(payload);
    return map;
  }

  TurnRowsCompanion toCompanion(bool nullToAbsent) {
    return TurnRowsCompanion(
      botId: Value(botId),
      eventId: Value(eventId),
      sequence: Value(sequence),
      payload: Value(payload),
    );
  }

  factory TurnRow.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return TurnRow(
      botId: serializer.fromJson<String>(json['botId']),
      eventId: serializer.fromJson<String>(json['eventId']),
      sequence: serializer.fromJson<int>(json['sequence']),
      payload: serializer.fromJson<Uint8List>(json['payload']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'botId': serializer.toJson<String>(botId),
      'eventId': serializer.toJson<String>(eventId),
      'sequence': serializer.toJson<int>(sequence),
      'payload': serializer.toJson<Uint8List>(payload),
    };
  }

  TurnRow copyWith({
    String? botId,
    String? eventId,
    int? sequence,
    Uint8List? payload,
  }) => TurnRow(
    botId: botId ?? this.botId,
    eventId: eventId ?? this.eventId,
    sequence: sequence ?? this.sequence,
    payload: payload ?? this.payload,
  );
  TurnRow copyWithCompanion(TurnRowsCompanion data) {
    return TurnRow(
      botId: data.botId.present ? data.botId.value : this.botId,
      eventId: data.eventId.present ? data.eventId.value : this.eventId,
      sequence: data.sequence.present ? data.sequence.value : this.sequence,
      payload: data.payload.present ? data.payload.value : this.payload,
    );
  }

  @override
  String toString() {
    return (StringBuffer('TurnRow(')
          ..write('botId: $botId, ')
          ..write('eventId: $eventId, ')
          ..write('sequence: $sequence, ')
          ..write('payload: $payload')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode =>
      Object.hash(botId, eventId, sequence, $driftBlobEquality.hash(payload));
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is TurnRow &&
          other.botId == this.botId &&
          other.eventId == this.eventId &&
          other.sequence == this.sequence &&
          $driftBlobEquality.equals(other.payload, this.payload));
}

class TurnRowsCompanion extends UpdateCompanion<TurnRow> {
  final Value<String> botId;
  final Value<String> eventId;
  final Value<int> sequence;
  final Value<Uint8List> payload;
  final Value<int> rowid;
  const TurnRowsCompanion({
    this.botId = const Value.absent(),
    this.eventId = const Value.absent(),
    this.sequence = const Value.absent(),
    this.payload = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  TurnRowsCompanion.insert({
    required String botId,
    required String eventId,
    required int sequence,
    required Uint8List payload,
    this.rowid = const Value.absent(),
  }) : botId = Value(botId),
       eventId = Value(eventId),
       sequence = Value(sequence),
       payload = Value(payload);
  static Insertable<TurnRow> custom({
    Expression<String>? botId,
    Expression<String>? eventId,
    Expression<int>? sequence,
    Expression<Uint8List>? payload,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (botId != null) 'bot_id': botId,
      if (eventId != null) 'event_id': eventId,
      if (sequence != null) 'sequence': sequence,
      if (payload != null) 'payload': payload,
      if (rowid != null) 'rowid': rowid,
    });
  }

  TurnRowsCompanion copyWith({
    Value<String>? botId,
    Value<String>? eventId,
    Value<int>? sequence,
    Value<Uint8List>? payload,
    Value<int>? rowid,
  }) {
    return TurnRowsCompanion(
      botId: botId ?? this.botId,
      eventId: eventId ?? this.eventId,
      sequence: sequence ?? this.sequence,
      payload: payload ?? this.payload,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (botId.present) {
      map['bot_id'] = Variable<String>(botId.value);
    }
    if (eventId.present) {
      map['event_id'] = Variable<String>(eventId.value);
    }
    if (sequence.present) {
      map['sequence'] = Variable<int>(sequence.value);
    }
    if (payload.present) {
      map['payload'] = Variable<Uint8List>(payload.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('TurnRowsCompanion(')
          ..write('botId: $botId, ')
          ..write('eventId: $eventId, ')
          ..write('sequence: $sequence, ')
          ..write('payload: $payload, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $QueueRowsTable extends QueueRows
    with TableInfo<$QueueRowsTable, QueueRow> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $QueueRowsTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _botIdMeta = const VerificationMeta('botId');
  @override
  late final GeneratedColumn<String> botId = GeneratedColumn<String>(
    'bot_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'REFERENCES bot_rows (bot_id) ON DELETE CASCADE',
    ),
  );
  static const VerificationMeta _requestIdMeta = const VerificationMeta(
    'requestId',
  );
  @override
  late final GeneratedColumn<String> requestId = GeneratedColumn<String>(
    'request_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _eventIdMeta = const VerificationMeta(
    'eventId',
  );
  @override
  late final GeneratedColumn<String> eventId = GeneratedColumn<String>(
    'event_id',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _payloadMeta = const VerificationMeta(
    'payload',
  );
  @override
  late final GeneratedColumn<Uint8List> payload = GeneratedColumn<Uint8List>(
    'payload',
    aliasedName,
    false,
    type: DriftSqlType.blob,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _admittedMeta = const VerificationMeta(
    'admitted',
  );
  @override
  late final GeneratedColumn<bool> admitted = GeneratedColumn<bool>(
    'admitted',
    aliasedName,
    false,
    type: DriftSqlType.bool,
    requiredDuringInsert: false,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'CHECK ("admitted" IN (0, 1))',
    ),
    defaultValue: const Constant(false),
  );
  static const VerificationMeta _errorMeta = const VerificationMeta('error');
  @override
  late final GeneratedColumn<String> error = GeneratedColumn<String>(
    'error',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _ordinalMeta = const VerificationMeta(
    'ordinal',
  );
  @override
  late final GeneratedColumn<int> ordinal = GeneratedColumn<int>(
    'ordinal',
    aliasedName,
    false,
    type: DriftSqlType.int,
    requiredDuringInsert: true,
  );
  @override
  List<GeneratedColumn> get $columns => [
    botId,
    requestId,
    eventId,
    payload,
    admitted,
    error,
    ordinal,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'queue_rows';
  @override
  VerificationContext validateIntegrity(
    Insertable<QueueRow> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('bot_id')) {
      context.handle(
        _botIdMeta,
        botId.isAcceptableOrUnknown(data['bot_id']!, _botIdMeta),
      );
    } else if (isInserting) {
      context.missing(_botIdMeta);
    }
    if (data.containsKey('request_id')) {
      context.handle(
        _requestIdMeta,
        requestId.isAcceptableOrUnknown(data['request_id']!, _requestIdMeta),
      );
    } else if (isInserting) {
      context.missing(_requestIdMeta);
    }
    if (data.containsKey('event_id')) {
      context.handle(
        _eventIdMeta,
        eventId.isAcceptableOrUnknown(data['event_id']!, _eventIdMeta),
      );
    }
    if (data.containsKey('payload')) {
      context.handle(
        _payloadMeta,
        payload.isAcceptableOrUnknown(data['payload']!, _payloadMeta),
      );
    } else if (isInserting) {
      context.missing(_payloadMeta);
    }
    if (data.containsKey('admitted')) {
      context.handle(
        _admittedMeta,
        admitted.isAcceptableOrUnknown(data['admitted']!, _admittedMeta),
      );
    }
    if (data.containsKey('error')) {
      context.handle(
        _errorMeta,
        error.isAcceptableOrUnknown(data['error']!, _errorMeta),
      );
    }
    if (data.containsKey('ordinal')) {
      context.handle(
        _ordinalMeta,
        ordinal.isAcceptableOrUnknown(data['ordinal']!, _ordinalMeta),
      );
    } else if (isInserting) {
      context.missing(_ordinalMeta);
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {botId, requestId};
  @override
  QueueRow map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return QueueRow(
      botId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}bot_id'],
      )!,
      requestId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}request_id'],
      )!,
      eventId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}event_id'],
      ),
      payload: attachedDatabase.typeMapping.read(
        DriftSqlType.blob,
        data['${effectivePrefix}payload'],
      )!,
      admitted: attachedDatabase.typeMapping.read(
        DriftSqlType.bool,
        data['${effectivePrefix}admitted'],
      )!,
      error: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}error'],
      ),
      ordinal: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}ordinal'],
      )!,
    );
  }

  @override
  $QueueRowsTable createAlias(String alias) {
    return $QueueRowsTable(attachedDatabase, alias);
  }
}

class QueueRow extends DataClass implements Insertable<QueueRow> {
  final String botId;
  final String requestId;
  final String? eventId;
  final Uint8List payload;
  final bool admitted;
  final String? error;
  final int ordinal;
  const QueueRow({
    required this.botId,
    required this.requestId,
    this.eventId,
    required this.payload,
    required this.admitted,
    this.error,
    required this.ordinal,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['bot_id'] = Variable<String>(botId);
    map['request_id'] = Variable<String>(requestId);
    if (!nullToAbsent || eventId != null) {
      map['event_id'] = Variable<String>(eventId);
    }
    map['payload'] = Variable<Uint8List>(payload);
    map['admitted'] = Variable<bool>(admitted);
    if (!nullToAbsent || error != null) {
      map['error'] = Variable<String>(error);
    }
    map['ordinal'] = Variable<int>(ordinal);
    return map;
  }

  QueueRowsCompanion toCompanion(bool nullToAbsent) {
    return QueueRowsCompanion(
      botId: Value(botId),
      requestId: Value(requestId),
      eventId: eventId == null && nullToAbsent
          ? const Value.absent()
          : Value(eventId),
      payload: Value(payload),
      admitted: Value(admitted),
      error: error == null && nullToAbsent
          ? const Value.absent()
          : Value(error),
      ordinal: Value(ordinal),
    );
  }

  factory QueueRow.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return QueueRow(
      botId: serializer.fromJson<String>(json['botId']),
      requestId: serializer.fromJson<String>(json['requestId']),
      eventId: serializer.fromJson<String?>(json['eventId']),
      payload: serializer.fromJson<Uint8List>(json['payload']),
      admitted: serializer.fromJson<bool>(json['admitted']),
      error: serializer.fromJson<String?>(json['error']),
      ordinal: serializer.fromJson<int>(json['ordinal']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'botId': serializer.toJson<String>(botId),
      'requestId': serializer.toJson<String>(requestId),
      'eventId': serializer.toJson<String?>(eventId),
      'payload': serializer.toJson<Uint8List>(payload),
      'admitted': serializer.toJson<bool>(admitted),
      'error': serializer.toJson<String?>(error),
      'ordinal': serializer.toJson<int>(ordinal),
    };
  }

  QueueRow copyWith({
    String? botId,
    String? requestId,
    Value<String?> eventId = const Value.absent(),
    Uint8List? payload,
    bool? admitted,
    Value<String?> error = const Value.absent(),
    int? ordinal,
  }) => QueueRow(
    botId: botId ?? this.botId,
    requestId: requestId ?? this.requestId,
    eventId: eventId.present ? eventId.value : this.eventId,
    payload: payload ?? this.payload,
    admitted: admitted ?? this.admitted,
    error: error.present ? error.value : this.error,
    ordinal: ordinal ?? this.ordinal,
  );
  QueueRow copyWithCompanion(QueueRowsCompanion data) {
    return QueueRow(
      botId: data.botId.present ? data.botId.value : this.botId,
      requestId: data.requestId.present ? data.requestId.value : this.requestId,
      eventId: data.eventId.present ? data.eventId.value : this.eventId,
      payload: data.payload.present ? data.payload.value : this.payload,
      admitted: data.admitted.present ? data.admitted.value : this.admitted,
      error: data.error.present ? data.error.value : this.error,
      ordinal: data.ordinal.present ? data.ordinal.value : this.ordinal,
    );
  }

  @override
  String toString() {
    return (StringBuffer('QueueRow(')
          ..write('botId: $botId, ')
          ..write('requestId: $requestId, ')
          ..write('eventId: $eventId, ')
          ..write('payload: $payload, ')
          ..write('admitted: $admitted, ')
          ..write('error: $error, ')
          ..write('ordinal: $ordinal')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(
    botId,
    requestId,
    eventId,
    $driftBlobEquality.hash(payload),
    admitted,
    error,
    ordinal,
  );
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is QueueRow &&
          other.botId == this.botId &&
          other.requestId == this.requestId &&
          other.eventId == this.eventId &&
          $driftBlobEquality.equals(other.payload, this.payload) &&
          other.admitted == this.admitted &&
          other.error == this.error &&
          other.ordinal == this.ordinal);
}

class QueueRowsCompanion extends UpdateCompanion<QueueRow> {
  final Value<String> botId;
  final Value<String> requestId;
  final Value<String?> eventId;
  final Value<Uint8List> payload;
  final Value<bool> admitted;
  final Value<String?> error;
  final Value<int> ordinal;
  final Value<int> rowid;
  const QueueRowsCompanion({
    this.botId = const Value.absent(),
    this.requestId = const Value.absent(),
    this.eventId = const Value.absent(),
    this.payload = const Value.absent(),
    this.admitted = const Value.absent(),
    this.error = const Value.absent(),
    this.ordinal = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  QueueRowsCompanion.insert({
    required String botId,
    required String requestId,
    this.eventId = const Value.absent(),
    required Uint8List payload,
    this.admitted = const Value.absent(),
    this.error = const Value.absent(),
    required int ordinal,
    this.rowid = const Value.absent(),
  }) : botId = Value(botId),
       requestId = Value(requestId),
       payload = Value(payload),
       ordinal = Value(ordinal);
  static Insertable<QueueRow> custom({
    Expression<String>? botId,
    Expression<String>? requestId,
    Expression<String>? eventId,
    Expression<Uint8List>? payload,
    Expression<bool>? admitted,
    Expression<String>? error,
    Expression<int>? ordinal,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (botId != null) 'bot_id': botId,
      if (requestId != null) 'request_id': requestId,
      if (eventId != null) 'event_id': eventId,
      if (payload != null) 'payload': payload,
      if (admitted != null) 'admitted': admitted,
      if (error != null) 'error': error,
      if (ordinal != null) 'ordinal': ordinal,
      if (rowid != null) 'rowid': rowid,
    });
  }

  QueueRowsCompanion copyWith({
    Value<String>? botId,
    Value<String>? requestId,
    Value<String?>? eventId,
    Value<Uint8List>? payload,
    Value<bool>? admitted,
    Value<String?>? error,
    Value<int>? ordinal,
    Value<int>? rowid,
  }) {
    return QueueRowsCompanion(
      botId: botId ?? this.botId,
      requestId: requestId ?? this.requestId,
      eventId: eventId ?? this.eventId,
      payload: payload ?? this.payload,
      admitted: admitted ?? this.admitted,
      error: error ?? this.error,
      ordinal: ordinal ?? this.ordinal,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (botId.present) {
      map['bot_id'] = Variable<String>(botId.value);
    }
    if (requestId.present) {
      map['request_id'] = Variable<String>(requestId.value);
    }
    if (eventId.present) {
      map['event_id'] = Variable<String>(eventId.value);
    }
    if (payload.present) {
      map['payload'] = Variable<Uint8List>(payload.value);
    }
    if (admitted.present) {
      map['admitted'] = Variable<bool>(admitted.value);
    }
    if (error.present) {
      map['error'] = Variable<String>(error.value);
    }
    if (ordinal.present) {
      map['ordinal'] = Variable<int>(ordinal.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('QueueRowsCompanion(')
          ..write('botId: $botId, ')
          ..write('requestId: $requestId, ')
          ..write('eventId: $eventId, ')
          ..write('payload: $payload, ')
          ..write('admitted: $admitted, ')
          ..write('error: $error, ')
          ..write('ordinal: $ordinal, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $MetaRowsTable extends MetaRows with TableInfo<$MetaRowsTable, MetaRow> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $MetaRowsTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _botIdMeta = const VerificationMeta('botId');
  @override
  late final GeneratedColumn<String> botId = GeneratedColumn<String>(
    'bot_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'REFERENCES bot_rows (bot_id) ON DELETE CASCADE',
    ),
  );
  static const VerificationMeta _nameMeta = const VerificationMeta('name');
  @override
  late final GeneratedColumn<String> name = GeneratedColumn<String>(
    'name',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _payloadMeta = const VerificationMeta(
    'payload',
  );
  @override
  late final GeneratedColumn<Uint8List> payload = GeneratedColumn<Uint8List>(
    'payload',
    aliasedName,
    false,
    type: DriftSqlType.blob,
    requiredDuringInsert: true,
  );
  @override
  List<GeneratedColumn> get $columns => [botId, name, payload];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'meta_rows';
  @override
  VerificationContext validateIntegrity(
    Insertable<MetaRow> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('bot_id')) {
      context.handle(
        _botIdMeta,
        botId.isAcceptableOrUnknown(data['bot_id']!, _botIdMeta),
      );
    } else if (isInserting) {
      context.missing(_botIdMeta);
    }
    if (data.containsKey('name')) {
      context.handle(
        _nameMeta,
        name.isAcceptableOrUnknown(data['name']!, _nameMeta),
      );
    } else if (isInserting) {
      context.missing(_nameMeta);
    }
    if (data.containsKey('payload')) {
      context.handle(
        _payloadMeta,
        payload.isAcceptableOrUnknown(data['payload']!, _payloadMeta),
      );
    } else if (isInserting) {
      context.missing(_payloadMeta);
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {botId, name};
  @override
  MetaRow map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return MetaRow(
      botId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}bot_id'],
      )!,
      name: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}name'],
      )!,
      payload: attachedDatabase.typeMapping.read(
        DriftSqlType.blob,
        data['${effectivePrefix}payload'],
      )!,
    );
  }

  @override
  $MetaRowsTable createAlias(String alias) {
    return $MetaRowsTable(attachedDatabase, alias);
  }
}

class MetaRow extends DataClass implements Insertable<MetaRow> {
  final String botId;
  final String name;
  final Uint8List payload;
  const MetaRow({
    required this.botId,
    required this.name,
    required this.payload,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['bot_id'] = Variable<String>(botId);
    map['name'] = Variable<String>(name);
    map['payload'] = Variable<Uint8List>(payload);
    return map;
  }

  MetaRowsCompanion toCompanion(bool nullToAbsent) {
    return MetaRowsCompanion(
      botId: Value(botId),
      name: Value(name),
      payload: Value(payload),
    );
  }

  factory MetaRow.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return MetaRow(
      botId: serializer.fromJson<String>(json['botId']),
      name: serializer.fromJson<String>(json['name']),
      payload: serializer.fromJson<Uint8List>(json['payload']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'botId': serializer.toJson<String>(botId),
      'name': serializer.toJson<String>(name),
      'payload': serializer.toJson<Uint8List>(payload),
    };
  }

  MetaRow copyWith({String? botId, String? name, Uint8List? payload}) =>
      MetaRow(
        botId: botId ?? this.botId,
        name: name ?? this.name,
        payload: payload ?? this.payload,
      );
  MetaRow copyWithCompanion(MetaRowsCompanion data) {
    return MetaRow(
      botId: data.botId.present ? data.botId.value : this.botId,
      name: data.name.present ? data.name.value : this.name,
      payload: data.payload.present ? data.payload.value : this.payload,
    );
  }

  @override
  String toString() {
    return (StringBuffer('MetaRow(')
          ..write('botId: $botId, ')
          ..write('name: $name, ')
          ..write('payload: $payload')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode =>
      Object.hash(botId, name, $driftBlobEquality.hash(payload));
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is MetaRow &&
          other.botId == this.botId &&
          other.name == this.name &&
          $driftBlobEquality.equals(other.payload, this.payload));
}

class MetaRowsCompanion extends UpdateCompanion<MetaRow> {
  final Value<String> botId;
  final Value<String> name;
  final Value<Uint8List> payload;
  final Value<int> rowid;
  const MetaRowsCompanion({
    this.botId = const Value.absent(),
    this.name = const Value.absent(),
    this.payload = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  MetaRowsCompanion.insert({
    required String botId,
    required String name,
    required Uint8List payload,
    this.rowid = const Value.absent(),
  }) : botId = Value(botId),
       name = Value(name),
       payload = Value(payload);
  static Insertable<MetaRow> custom({
    Expression<String>? botId,
    Expression<String>? name,
    Expression<Uint8List>? payload,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (botId != null) 'bot_id': botId,
      if (name != null) 'name': name,
      if (payload != null) 'payload': payload,
      if (rowid != null) 'rowid': rowid,
    });
  }

  MetaRowsCompanion copyWith({
    Value<String>? botId,
    Value<String>? name,
    Value<Uint8List>? payload,
    Value<int>? rowid,
  }) {
    return MetaRowsCompanion(
      botId: botId ?? this.botId,
      name: name ?? this.name,
      payload: payload ?? this.payload,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (botId.present) {
      map['bot_id'] = Variable<String>(botId.value);
    }
    if (name.present) {
      map['name'] = Variable<String>(name.value);
    }
    if (payload.present) {
      map['payload'] = Variable<Uint8List>(payload.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('MetaRowsCompanion(')
          ..write('botId: $botId, ')
          ..write('name: $name, ')
          ..write('payload: $payload, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $ViewedRepliesTable extends ViewedReplies
    with TableInfo<$ViewedRepliesTable, ViewedReply> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $ViewedRepliesTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _botIdMeta = const VerificationMeta('botId');
  @override
  late final GeneratedColumn<String> botId = GeneratedColumn<String>(
    'bot_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'REFERENCES bot_rows (bot_id) ON DELETE CASCADE',
    ),
  );
  static const VerificationMeta _messageIdMeta = const VerificationMeta(
    'messageId',
  );
  @override
  late final GeneratedColumn<String> messageId = GeneratedColumn<String>(
    'message_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  @override
  List<GeneratedColumn> get $columns => [botId, messageId];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'viewed_replies';
  @override
  VerificationContext validateIntegrity(
    Insertable<ViewedReply> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('bot_id')) {
      context.handle(
        _botIdMeta,
        botId.isAcceptableOrUnknown(data['bot_id']!, _botIdMeta),
      );
    } else if (isInserting) {
      context.missing(_botIdMeta);
    }
    if (data.containsKey('message_id')) {
      context.handle(
        _messageIdMeta,
        messageId.isAcceptableOrUnknown(data['message_id']!, _messageIdMeta),
      );
    } else if (isInserting) {
      context.missing(_messageIdMeta);
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {botId, messageId};
  @override
  ViewedReply map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return ViewedReply(
      botId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}bot_id'],
      )!,
      messageId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}message_id'],
      )!,
    );
  }

  @override
  $ViewedRepliesTable createAlias(String alias) {
    return $ViewedRepliesTable(attachedDatabase, alias);
  }
}

class ViewedReply extends DataClass implements Insertable<ViewedReply> {
  final String botId;
  final String messageId;
  const ViewedReply({required this.botId, required this.messageId});
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['bot_id'] = Variable<String>(botId);
    map['message_id'] = Variable<String>(messageId);
    return map;
  }

  ViewedRepliesCompanion toCompanion(bool nullToAbsent) {
    return ViewedRepliesCompanion(
      botId: Value(botId),
      messageId: Value(messageId),
    );
  }

  factory ViewedReply.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return ViewedReply(
      botId: serializer.fromJson<String>(json['botId']),
      messageId: serializer.fromJson<String>(json['messageId']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'botId': serializer.toJson<String>(botId),
      'messageId': serializer.toJson<String>(messageId),
    };
  }

  ViewedReply copyWith({String? botId, String? messageId}) => ViewedReply(
    botId: botId ?? this.botId,
    messageId: messageId ?? this.messageId,
  );
  ViewedReply copyWithCompanion(ViewedRepliesCompanion data) {
    return ViewedReply(
      botId: data.botId.present ? data.botId.value : this.botId,
      messageId: data.messageId.present ? data.messageId.value : this.messageId,
    );
  }

  @override
  String toString() {
    return (StringBuffer('ViewedReply(')
          ..write('botId: $botId, ')
          ..write('messageId: $messageId')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(botId, messageId);
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is ViewedReply &&
          other.botId == this.botId &&
          other.messageId == this.messageId);
}

class ViewedRepliesCompanion extends UpdateCompanion<ViewedReply> {
  final Value<String> botId;
  final Value<String> messageId;
  final Value<int> rowid;
  const ViewedRepliesCompanion({
    this.botId = const Value.absent(),
    this.messageId = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  ViewedRepliesCompanion.insert({
    required String botId,
    required String messageId,
    this.rowid = const Value.absent(),
  }) : botId = Value(botId),
       messageId = Value(messageId);
  static Insertable<ViewedReply> custom({
    Expression<String>? botId,
    Expression<String>? messageId,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (botId != null) 'bot_id': botId,
      if (messageId != null) 'message_id': messageId,
      if (rowid != null) 'rowid': rowid,
    });
  }

  ViewedRepliesCompanion copyWith({
    Value<String>? botId,
    Value<String>? messageId,
    Value<int>? rowid,
  }) {
    return ViewedRepliesCompanion(
      botId: botId ?? this.botId,
      messageId: messageId ?? this.messageId,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (botId.present) {
      map['bot_id'] = Variable<String>(botId.value);
    }
    if (messageId.present) {
      map['message_id'] = Variable<String>(messageId.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('ViewedRepliesCompanion(')
          ..write('botId: $botId, ')
          ..write('messageId: $messageId, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

abstract class _$ChatDatabase extends GeneratedDatabase {
  _$ChatDatabase(QueryExecutor e) : super(e);
  $ChatDatabaseManager get managers => $ChatDatabaseManager(this);
  late final $BotRowsTable botRows = $BotRowsTable(this);
  late final $TurnRowsTable turnRows = $TurnRowsTable(this);
  late final $QueueRowsTable queueRows = $QueueRowsTable(this);
  late final $MetaRowsTable metaRows = $MetaRowsTable(this);
  late final $ViewedRepliesTable viewedReplies = $ViewedRepliesTable(this);
  @override
  Iterable<TableInfo<Table, Object?>> get allTables =>
      allSchemaEntities.whereType<TableInfo<Table, Object?>>();
  @override
  List<DatabaseSchemaEntity> get allSchemaEntities => [
    botRows,
    turnRows,
    queueRows,
    metaRows,
    viewedReplies,
  ];
  @override
  StreamQueryUpdateRules get streamUpdateRules => const StreamQueryUpdateRules([
    WritePropagation(
      on: TableUpdateQuery.onTableName(
        'bot_rows',
        limitUpdateKind: UpdateKind.delete,
      ),
      result: [TableUpdate('turn_rows', kind: UpdateKind.delete)],
    ),
    WritePropagation(
      on: TableUpdateQuery.onTableName(
        'bot_rows',
        limitUpdateKind: UpdateKind.delete,
      ),
      result: [TableUpdate('queue_rows', kind: UpdateKind.delete)],
    ),
    WritePropagation(
      on: TableUpdateQuery.onTableName(
        'bot_rows',
        limitUpdateKind: UpdateKind.delete,
      ),
      result: [TableUpdate('meta_rows', kind: UpdateKind.delete)],
    ),
    WritePropagation(
      on: TableUpdateQuery.onTableName(
        'bot_rows',
        limitUpdateKind: UpdateKind.delete,
      ),
      result: [TableUpdate('viewed_replies', kind: UpdateKind.delete)],
    ),
  ]);
}

typedef $$BotRowsTableCreateCompanionBuilder = BotRowsCompanion Function({
  required String botId,
  required String displayName,
  required String backend,
  required String endpoint,
  Value<int> rowid,
});
typedef $$BotRowsTableUpdateCompanionBuilder = BotRowsCompanion Function({
  Value<String> botId,
  Value<String> displayName,
  Value<String> backend,
  Value<String> endpoint,
  Value<int> rowid,
});

final class $$BotRowsTableReferences
    extends BaseReferences<_$ChatDatabase, $BotRowsTable, BotRow> {
  $$BotRowsTableReferences(super.$_db, super.$_table, super.$_typedResult);

  static MultiTypedResultKey<$TurnRowsTable, List<TurnRow>> _turnRowsRefsTable(
    _$ChatDatabase db,
  ) => MultiTypedResultKey.fromTable(
    db.turnRows,
    aliasName: 'bot_rows__bot_id__turn_rows__bot_id',
  );

  $$TurnRowsTableProcessedTableManager get turnRowsRefs {
    final manager = $$TurnRowsTableTableManager(
      $_db,
      $_db.turnRows,
    ).filter((f) => f.botId.botId.sqlEquals($_itemColumn<String>('bot_id')!));

    final cache = $_typedResult.readTableOrNull(_turnRowsRefsTable($_db));
    return ProcessedTableManager(
      manager.$state.copyWith(prefetchedData: cache),
    );
  }

  static MultiTypedResultKey<$QueueRowsTable, List<QueueRow>>
  _queueRowsRefsTable(_$ChatDatabase db) => MultiTypedResultKey.fromTable(
    db.queueRows,
    aliasName: 'bot_rows__bot_id__queue_rows__bot_id',
  );

  $$QueueRowsTableProcessedTableManager get queueRowsRefs {
    final manager = $$QueueRowsTableTableManager(
      $_db,
      $_db.queueRows,
    ).filter((f) => f.botId.botId.sqlEquals($_itemColumn<String>('bot_id')!));

    final cache = $_typedResult.readTableOrNull(_queueRowsRefsTable($_db));
    return ProcessedTableManager(
      manager.$state.copyWith(prefetchedData: cache),
    );
  }

  static MultiTypedResultKey<$MetaRowsTable, List<MetaRow>> _metaRowsRefsTable(
    _$ChatDatabase db,
  ) => MultiTypedResultKey.fromTable(
    db.metaRows,
    aliasName: 'bot_rows__bot_id__meta_rows__bot_id',
  );

  $$MetaRowsTableProcessedTableManager get metaRowsRefs {
    final manager = $$MetaRowsTableTableManager(
      $_db,
      $_db.metaRows,
    ).filter((f) => f.botId.botId.sqlEquals($_itemColumn<String>('bot_id')!));

    final cache = $_typedResult.readTableOrNull(_metaRowsRefsTable($_db));
    return ProcessedTableManager(
      manager.$state.copyWith(prefetchedData: cache),
    );
  }

  static MultiTypedResultKey<$ViewedRepliesTable, List<ViewedReply>>
  _viewedRepliesRefsTable(_$ChatDatabase db) => MultiTypedResultKey.fromTable(
    db.viewedReplies,
    aliasName: 'bot_rows__bot_id__viewed_replies__bot_id',
  );

  $$ViewedRepliesTableProcessedTableManager get viewedRepliesRefs {
    final manager = $$ViewedRepliesTableTableManager(
      $_db,
      $_db.viewedReplies,
    ).filter((f) => f.botId.botId.sqlEquals($_itemColumn<String>('bot_id')!));

    final cache = $_typedResult.readTableOrNull(_viewedRepliesRefsTable($_db));
    return ProcessedTableManager(
      manager.$state.copyWith(prefetchedData: cache),
    );
  }
}

class $$BotRowsTableFilterComposer
    extends Composer<_$ChatDatabase, $BotRowsTable> {
  $$BotRowsTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get botId => $composableBuilder(
    column: $table.botId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get displayName => $composableBuilder(
    column: $table.displayName,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get backend => $composableBuilder(
    column: $table.backend,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get endpoint => $composableBuilder(
    column: $table.endpoint,
    builder: (column) => ColumnFilters(column),
  );

  Expression<bool> turnRowsRefs(
    Expression<bool> Function($$TurnRowsTableFilterComposer f) f,
  ) {
    final $$TurnRowsTableFilterComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.turnRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$TurnRowsTableFilterComposer(
            $db: $db,
            $table: $db.turnRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return f(composer);
  }

  Expression<bool> queueRowsRefs(
    Expression<bool> Function($$QueueRowsTableFilterComposer f) f,
  ) {
    final $$QueueRowsTableFilterComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.queueRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$QueueRowsTableFilterComposer(
            $db: $db,
            $table: $db.queueRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return f(composer);
  }

  Expression<bool> metaRowsRefs(
    Expression<bool> Function($$MetaRowsTableFilterComposer f) f,
  ) {
    final $$MetaRowsTableFilterComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.metaRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$MetaRowsTableFilterComposer(
            $db: $db,
            $table: $db.metaRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return f(composer);
  }

  Expression<bool> viewedRepliesRefs(
    Expression<bool> Function($$ViewedRepliesTableFilterComposer f) f,
  ) {
    final $$ViewedRepliesTableFilterComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.viewedReplies,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$ViewedRepliesTableFilterComposer(
            $db: $db,
            $table: $db.viewedReplies,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return f(composer);
  }
}

class $$BotRowsTableOrderingComposer
    extends Composer<_$ChatDatabase, $BotRowsTable> {
  $$BotRowsTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get botId => $composableBuilder(
    column: $table.botId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get displayName => $composableBuilder(
    column: $table.displayName,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get backend => $composableBuilder(
    column: $table.backend,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get endpoint => $composableBuilder(
    column: $table.endpoint,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$BotRowsTableAnnotationComposer
    extends Composer<_$ChatDatabase, $BotRowsTable> {
  $$BotRowsTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get botId =>
      $composableBuilder(column: $table.botId, builder: (column) => column);

  GeneratedColumn<String> get displayName => $composableBuilder(
    column: $table.displayName,
    builder: (column) => column,
  );

  GeneratedColumn<String> get backend =>
      $composableBuilder(column: $table.backend, builder: (column) => column);

  GeneratedColumn<String> get endpoint =>
      $composableBuilder(column: $table.endpoint, builder: (column) => column);

  Expression<T> turnRowsRefs<T extends Object>(
    Expression<T> Function($$TurnRowsTableAnnotationComposer a) f,
  ) {
    final $$TurnRowsTableAnnotationComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.turnRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$TurnRowsTableAnnotationComposer(
            $db: $db,
            $table: $db.turnRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return f(composer);
  }

  Expression<T> queueRowsRefs<T extends Object>(
    Expression<T> Function($$QueueRowsTableAnnotationComposer a) f,
  ) {
    final $$QueueRowsTableAnnotationComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.queueRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$QueueRowsTableAnnotationComposer(
            $db: $db,
            $table: $db.queueRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return f(composer);
  }

  Expression<T> metaRowsRefs<T extends Object>(
    Expression<T> Function($$MetaRowsTableAnnotationComposer a) f,
  ) {
    final $$MetaRowsTableAnnotationComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.metaRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$MetaRowsTableAnnotationComposer(
            $db: $db,
            $table: $db.metaRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return f(composer);
  }

  Expression<T> viewedRepliesRefs<T extends Object>(
    Expression<T> Function($$ViewedRepliesTableAnnotationComposer a) f,
  ) {
    final $$ViewedRepliesTableAnnotationComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.viewedReplies,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$ViewedRepliesTableAnnotationComposer(
            $db: $db,
            $table: $db.viewedReplies,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return f(composer);
  }
}

class $$BotRowsTableTableManager
    extends
        RootTableManager<
          _$ChatDatabase,
          $BotRowsTable,
          BotRow,
          $$BotRowsTableFilterComposer,
          $$BotRowsTableOrderingComposer,
          $$BotRowsTableAnnotationComposer,
          $$BotRowsTableCreateCompanionBuilder,
          $$BotRowsTableUpdateCompanionBuilder,
          (BotRow, $$BotRowsTableReferences),
          BotRow,
          PrefetchHooks Function({
            bool turnRowsRefs,
            bool queueRowsRefs,
            bool metaRowsRefs,
            bool viewedRepliesRefs,
          })
        > {
  $$BotRowsTableTableManager(_$ChatDatabase db, $BotRowsTable table)
    : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$BotRowsTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$BotRowsTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$BotRowsTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<String> botId = const Value.absent(),
                Value<String> displayName = const Value.absent(),
                Value<String> backend = const Value.absent(),
                Value<String> endpoint = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => BotRowsCompanion(
                botId: botId,
                displayName: displayName,
                backend: backend,
                endpoint: endpoint,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String botId,
                required String displayName,
                required String backend,
                required String endpoint,
                Value<int> rowid = const Value.absent(),
              }) => BotRowsCompanion.insert(
                botId: botId,
                displayName: displayName,
                backend: backend,
                endpoint: endpoint,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map(
                (e) => (
                  e.readTable<$BotRowsTable, BotRow>(table),
                  $$BotRowsTableReferences(db, table, e),
                ),
              )
              .toList(),
          prefetchHooksCallback:
              ({
                turnRowsRefs = false,
                queueRowsRefs = false,
                metaRowsRefs = false,
                viewedRepliesRefs = false,
              }) {
                return PrefetchHooks(
                  db: db,
                  explicitlyWatchedTables: [
                    if (turnRowsRefs) db.turnRows,
                    if (queueRowsRefs) db.queueRows,
                    if (metaRowsRefs) db.metaRows,
                    if (viewedRepliesRefs) db.viewedReplies,
                  ],
                  addJoins: null,
                  getPrefetchedDataCallback: (items) async {
                    return [
                      if (turnRowsRefs)
                        await $_getPrefetchedDataByColumns<
                          BotRow,
                          $BotRowsTable,
                          TurnRow,
                          $TurnRowsTable
                        >(
                          currentTable: table,
                          referencedTable: $$BotRowsTableReferences
                              ._turnRowsRefsTable(db),
                          managerFromTypedResult: (p0) =>
                              $$BotRowsTableReferences(
                                db,
                                table,
                                p0,
                              ).turnRowsRefs,
                          currentColumn: 'bot_id',
                          referencedColumn: 'bot_id',
                          typedResults: items,
                        ),
                      if (queueRowsRefs)
                        await $_getPrefetchedDataByColumns<
                          BotRow,
                          $BotRowsTable,
                          QueueRow,
                          $QueueRowsTable
                        >(
                          currentTable: table,
                          referencedTable: $$BotRowsTableReferences
                              ._queueRowsRefsTable(db),
                          managerFromTypedResult: (p0) =>
                              $$BotRowsTableReferences(
                                db,
                                table,
                                p0,
                              ).queueRowsRefs,
                          currentColumn: 'bot_id',
                          referencedColumn: 'bot_id',
                          typedResults: items,
                        ),
                      if (metaRowsRefs)
                        await $_getPrefetchedDataByColumns<
                          BotRow,
                          $BotRowsTable,
                          MetaRow,
                          $MetaRowsTable
                        >(
                          currentTable: table,
                          referencedTable: $$BotRowsTableReferences
                              ._metaRowsRefsTable(db),
                          managerFromTypedResult: (p0) =>
                              $$BotRowsTableReferences(
                                db,
                                table,
                                p0,
                              ).metaRowsRefs,
                          currentColumn: 'bot_id',
                          referencedColumn: 'bot_id',
                          typedResults: items,
                        ),
                      if (viewedRepliesRefs)
                        await $_getPrefetchedDataByColumns<
                          BotRow,
                          $BotRowsTable,
                          ViewedReply,
                          $ViewedRepliesTable
                        >(
                          currentTable: table,
                          referencedTable: $$BotRowsTableReferences
                              ._viewedRepliesRefsTable(db),
                          managerFromTypedResult: (p0) =>
                              $$BotRowsTableReferences(
                                db,
                                table,
                                p0,
                              ).viewedRepliesRefs,
                          currentColumn: 'bot_id',
                          referencedColumn: 'bot_id',
                          typedResults: items,
                        ),
                    ];
                  },
                );
              },
        ),
      );
}

typedef $$BotRowsTableProcessedTableManager =
    ProcessedTableManager<
      _$ChatDatabase,
      $BotRowsTable,
      BotRow,
      $$BotRowsTableFilterComposer,
      $$BotRowsTableOrderingComposer,
      $$BotRowsTableAnnotationComposer,
      $$BotRowsTableCreateCompanionBuilder,
      $$BotRowsTableUpdateCompanionBuilder,
      (BotRow, $$BotRowsTableReferences),
      BotRow,
      PrefetchHooks Function({
        bool turnRowsRefs,
        bool queueRowsRefs,
        bool metaRowsRefs,
        bool viewedRepliesRefs,
      })
    >;
typedef $$TurnRowsTableCreateCompanionBuilder = TurnRowsCompanion Function({
  required String botId,
  required String eventId,
  required int sequence,
  required Uint8List payload,
  Value<int> rowid,
});
typedef $$TurnRowsTableUpdateCompanionBuilder = TurnRowsCompanion Function({
  Value<String> botId,
  Value<String> eventId,
  Value<int> sequence,
  Value<Uint8List> payload,
  Value<int> rowid,
});

final class $$TurnRowsTableReferences
    extends BaseReferences<_$ChatDatabase, $TurnRowsTable, TurnRow> {
  $$TurnRowsTableReferences(super.$_db, super.$_table, super.$_typedResult);

  static $BotRowsTable _botIdTable(_$ChatDatabase db) =>
      db.botRows.createAlias('turn_rows__bot_id__bot_rows__bot_id');

  $$BotRowsTableProcessedTableManager get botId {
    final $_column = $_itemColumn<String>('bot_id')!;

    final manager = $$BotRowsTableTableManager(
      $_db,
      $_db.botRows,
    ).filter((f) => f.botId.sqlEquals($_column));
    final item = $_typedResult.readTableOrNull(_botIdTable($_db));
    if (item == null) return manager;
    return ProcessedTableManager(
      manager.$state.copyWith(prefetchedData: [item]),
    );
  }
}

class $$TurnRowsTableFilterComposer
    extends Composer<_$ChatDatabase, $TurnRowsTable> {
  $$TurnRowsTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get eventId => $composableBuilder(
    column: $table.eventId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get sequence => $composableBuilder(
    column: $table.sequence,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<Uint8List> get payload => $composableBuilder(
    column: $table.payload,
    builder: (column) => ColumnFilters(column),
  );

  $$BotRowsTableFilterComposer get botId {
    final $$BotRowsTableFilterComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.botRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$BotRowsTableFilterComposer(
            $db: $db,
            $table: $db.botRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$TurnRowsTableOrderingComposer
    extends Composer<_$ChatDatabase, $TurnRowsTable> {
  $$TurnRowsTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get eventId => $composableBuilder(
    column: $table.eventId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get sequence => $composableBuilder(
    column: $table.sequence,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<Uint8List> get payload => $composableBuilder(
    column: $table.payload,
    builder: (column) => ColumnOrderings(column),
  );

  $$BotRowsTableOrderingComposer get botId {
    final $$BotRowsTableOrderingComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.botRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$BotRowsTableOrderingComposer(
            $db: $db,
            $table: $db.botRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$TurnRowsTableAnnotationComposer
    extends Composer<_$ChatDatabase, $TurnRowsTable> {
  $$TurnRowsTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get eventId =>
      $composableBuilder(column: $table.eventId, builder: (column) => column);

  GeneratedColumn<int> get sequence =>
      $composableBuilder(column: $table.sequence, builder: (column) => column);

  GeneratedColumn<Uint8List> get payload =>
      $composableBuilder(column: $table.payload, builder: (column) => column);

  $$BotRowsTableAnnotationComposer get botId {
    final $$BotRowsTableAnnotationComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.botRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$BotRowsTableAnnotationComposer(
            $db: $db,
            $table: $db.botRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$TurnRowsTableTableManager
    extends
        RootTableManager<
          _$ChatDatabase,
          $TurnRowsTable,
          TurnRow,
          $$TurnRowsTableFilterComposer,
          $$TurnRowsTableOrderingComposer,
          $$TurnRowsTableAnnotationComposer,
          $$TurnRowsTableCreateCompanionBuilder,
          $$TurnRowsTableUpdateCompanionBuilder,
          (TurnRow, $$TurnRowsTableReferences),
          TurnRow,
          PrefetchHooks Function({bool botId})
        > {
  $$TurnRowsTableTableManager(_$ChatDatabase db, $TurnRowsTable table)
    : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$TurnRowsTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$TurnRowsTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$TurnRowsTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<String> botId = const Value.absent(),
                Value<String> eventId = const Value.absent(),
                Value<int> sequence = const Value.absent(),
                Value<Uint8List> payload = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => TurnRowsCompanion(
                botId: botId,
                eventId: eventId,
                sequence: sequence,
                payload: payload,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String botId,
                required String eventId,
                required int sequence,
                required Uint8List payload,
                Value<int> rowid = const Value.absent(),
              }) => TurnRowsCompanion.insert(
                botId: botId,
                eventId: eventId,
                sequence: sequence,
                payload: payload,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map(
                (e) => (
                  e.readTable<$TurnRowsTable, TurnRow>(table),
                  $$TurnRowsTableReferences(db, table, e),
                ),
              )
              .toList(),
          prefetchHooksCallback: ({botId = false}) {
            return PrefetchHooks(
              db: db,
              explicitlyWatchedTables: [],
              addJoins:
                  <
                    T extends TableManagerState<
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic
                    >
                  >(state) {
                    if (botId) {
                      state = state.withJoin(
                        currentTable: table,
                        currentColumn: table.botId,
                        referencedTable: $$TurnRowsTableReferences._botIdTable(
                          db,
                        ),
                        referencedColumn: $$TurnRowsTableReferences
                            ._botIdTable(db)
                            .botId,
                      ) as T;
                    }

                    return state;
                  },
              getPrefetchedDataCallback: (items) async {
                return [];
              },
            );
          },
        ),
      );
}

typedef $$TurnRowsTableProcessedTableManager =
    ProcessedTableManager<
      _$ChatDatabase,
      $TurnRowsTable,
      TurnRow,
      $$TurnRowsTableFilterComposer,
      $$TurnRowsTableOrderingComposer,
      $$TurnRowsTableAnnotationComposer,
      $$TurnRowsTableCreateCompanionBuilder,
      $$TurnRowsTableUpdateCompanionBuilder,
      (TurnRow, $$TurnRowsTableReferences),
      TurnRow,
      PrefetchHooks Function({bool botId})
    >;
typedef $$QueueRowsTableCreateCompanionBuilder = QueueRowsCompanion Function({
  required String botId,
  required String requestId,
  Value<String?> eventId,
  required Uint8List payload,
  Value<bool> admitted,
  Value<String?> error,
  required int ordinal,
  Value<int> rowid,
});
typedef $$QueueRowsTableUpdateCompanionBuilder = QueueRowsCompanion Function({
  Value<String> botId,
  Value<String> requestId,
  Value<String?> eventId,
  Value<Uint8List> payload,
  Value<bool> admitted,
  Value<String?> error,
  Value<int> ordinal,
  Value<int> rowid,
});

final class $$QueueRowsTableReferences
    extends BaseReferences<_$ChatDatabase, $QueueRowsTable, QueueRow> {
  $$QueueRowsTableReferences(super.$_db, super.$_table, super.$_typedResult);

  static $BotRowsTable _botIdTable(_$ChatDatabase db) =>
      db.botRows.createAlias('queue_rows__bot_id__bot_rows__bot_id');

  $$BotRowsTableProcessedTableManager get botId {
    final $_column = $_itemColumn<String>('bot_id')!;

    final manager = $$BotRowsTableTableManager(
      $_db,
      $_db.botRows,
    ).filter((f) => f.botId.sqlEquals($_column));
    final item = $_typedResult.readTableOrNull(_botIdTable($_db));
    if (item == null) return manager;
    return ProcessedTableManager(
      manager.$state.copyWith(prefetchedData: [item]),
    );
  }
}

class $$QueueRowsTableFilterComposer
    extends Composer<_$ChatDatabase, $QueueRowsTable> {
  $$QueueRowsTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get requestId => $composableBuilder(
    column: $table.requestId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get eventId => $composableBuilder(
    column: $table.eventId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<Uint8List> get payload => $composableBuilder(
    column: $table.payload,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<bool> get admitted => $composableBuilder(
    column: $table.admitted,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get error => $composableBuilder(
    column: $table.error,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get ordinal => $composableBuilder(
    column: $table.ordinal,
    builder: (column) => ColumnFilters(column),
  );

  $$BotRowsTableFilterComposer get botId {
    final $$BotRowsTableFilterComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.botRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$BotRowsTableFilterComposer(
            $db: $db,
            $table: $db.botRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$QueueRowsTableOrderingComposer
    extends Composer<_$ChatDatabase, $QueueRowsTable> {
  $$QueueRowsTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get requestId => $composableBuilder(
    column: $table.requestId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get eventId => $composableBuilder(
    column: $table.eventId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<Uint8List> get payload => $composableBuilder(
    column: $table.payload,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<bool> get admitted => $composableBuilder(
    column: $table.admitted,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get error => $composableBuilder(
    column: $table.error,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get ordinal => $composableBuilder(
    column: $table.ordinal,
    builder: (column) => ColumnOrderings(column),
  );

  $$BotRowsTableOrderingComposer get botId {
    final $$BotRowsTableOrderingComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.botRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$BotRowsTableOrderingComposer(
            $db: $db,
            $table: $db.botRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$QueueRowsTableAnnotationComposer
    extends Composer<_$ChatDatabase, $QueueRowsTable> {
  $$QueueRowsTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get requestId =>
      $composableBuilder(column: $table.requestId, builder: (column) => column);

  GeneratedColumn<String> get eventId =>
      $composableBuilder(column: $table.eventId, builder: (column) => column);

  GeneratedColumn<Uint8List> get payload =>
      $composableBuilder(column: $table.payload, builder: (column) => column);

  GeneratedColumn<bool> get admitted =>
      $composableBuilder(column: $table.admitted, builder: (column) => column);

  GeneratedColumn<String> get error =>
      $composableBuilder(column: $table.error, builder: (column) => column);

  GeneratedColumn<int> get ordinal =>
      $composableBuilder(column: $table.ordinal, builder: (column) => column);

  $$BotRowsTableAnnotationComposer get botId {
    final $$BotRowsTableAnnotationComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.botRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$BotRowsTableAnnotationComposer(
            $db: $db,
            $table: $db.botRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$QueueRowsTableTableManager
    extends
        RootTableManager<
          _$ChatDatabase,
          $QueueRowsTable,
          QueueRow,
          $$QueueRowsTableFilterComposer,
          $$QueueRowsTableOrderingComposer,
          $$QueueRowsTableAnnotationComposer,
          $$QueueRowsTableCreateCompanionBuilder,
          $$QueueRowsTableUpdateCompanionBuilder,
          (QueueRow, $$QueueRowsTableReferences),
          QueueRow,
          PrefetchHooks Function({bool botId})
        > {
  $$QueueRowsTableTableManager(_$ChatDatabase db, $QueueRowsTable table)
    : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$QueueRowsTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$QueueRowsTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$QueueRowsTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<String> botId = const Value.absent(),
                Value<String> requestId = const Value.absent(),
                Value<String?> eventId = const Value.absent(),
                Value<Uint8List> payload = const Value.absent(),
                Value<bool> admitted = const Value.absent(),
                Value<String?> error = const Value.absent(),
                Value<int> ordinal = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => QueueRowsCompanion(
                botId: botId,
                requestId: requestId,
                eventId: eventId,
                payload: payload,
                admitted: admitted,
                error: error,
                ordinal: ordinal,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String botId,
                required String requestId,
                Value<String?> eventId = const Value.absent(),
                required Uint8List payload,
                Value<bool> admitted = const Value.absent(),
                Value<String?> error = const Value.absent(),
                required int ordinal,
                Value<int> rowid = const Value.absent(),
              }) => QueueRowsCompanion.insert(
                botId: botId,
                requestId: requestId,
                eventId: eventId,
                payload: payload,
                admitted: admitted,
                error: error,
                ordinal: ordinal,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map(
                (e) => (
                  e.readTable<$QueueRowsTable, QueueRow>(table),
                  $$QueueRowsTableReferences(db, table, e),
                ),
              )
              .toList(),
          prefetchHooksCallback: ({botId = false}) {
            return PrefetchHooks(
              db: db,
              explicitlyWatchedTables: [],
              addJoins:
                  <
                    T extends TableManagerState<
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic
                    >
                  >(state) {
                    if (botId) {
                      state = state.withJoin(
                        currentTable: table,
                        currentColumn: table.botId,
                        referencedTable: $$QueueRowsTableReferences._botIdTable(
                          db,
                        ),
                        referencedColumn: $$QueueRowsTableReferences
                            ._botIdTable(db)
                            .botId,
                      ) as T;
                    }

                    return state;
                  },
              getPrefetchedDataCallback: (items) async {
                return [];
              },
            );
          },
        ),
      );
}

typedef $$QueueRowsTableProcessedTableManager =
    ProcessedTableManager<
      _$ChatDatabase,
      $QueueRowsTable,
      QueueRow,
      $$QueueRowsTableFilterComposer,
      $$QueueRowsTableOrderingComposer,
      $$QueueRowsTableAnnotationComposer,
      $$QueueRowsTableCreateCompanionBuilder,
      $$QueueRowsTableUpdateCompanionBuilder,
      (QueueRow, $$QueueRowsTableReferences),
      QueueRow,
      PrefetchHooks Function({bool botId})
    >;
typedef $$MetaRowsTableCreateCompanionBuilder = MetaRowsCompanion Function({
  required String botId,
  required String name,
  required Uint8List payload,
  Value<int> rowid,
});
typedef $$MetaRowsTableUpdateCompanionBuilder = MetaRowsCompanion Function({
  Value<String> botId,
  Value<String> name,
  Value<Uint8List> payload,
  Value<int> rowid,
});

final class $$MetaRowsTableReferences
    extends BaseReferences<_$ChatDatabase, $MetaRowsTable, MetaRow> {
  $$MetaRowsTableReferences(super.$_db, super.$_table, super.$_typedResult);

  static $BotRowsTable _botIdTable(_$ChatDatabase db) =>
      db.botRows.createAlias('meta_rows__bot_id__bot_rows__bot_id');

  $$BotRowsTableProcessedTableManager get botId {
    final $_column = $_itemColumn<String>('bot_id')!;

    final manager = $$BotRowsTableTableManager(
      $_db,
      $_db.botRows,
    ).filter((f) => f.botId.sqlEquals($_column));
    final item = $_typedResult.readTableOrNull(_botIdTable($_db));
    if (item == null) return manager;
    return ProcessedTableManager(
      manager.$state.copyWith(prefetchedData: [item]),
    );
  }
}

class $$MetaRowsTableFilterComposer
    extends Composer<_$ChatDatabase, $MetaRowsTable> {
  $$MetaRowsTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get name => $composableBuilder(
    column: $table.name,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<Uint8List> get payload => $composableBuilder(
    column: $table.payload,
    builder: (column) => ColumnFilters(column),
  );

  $$BotRowsTableFilterComposer get botId {
    final $$BotRowsTableFilterComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.botRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$BotRowsTableFilterComposer(
            $db: $db,
            $table: $db.botRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$MetaRowsTableOrderingComposer
    extends Composer<_$ChatDatabase, $MetaRowsTable> {
  $$MetaRowsTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get name => $composableBuilder(
    column: $table.name,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<Uint8List> get payload => $composableBuilder(
    column: $table.payload,
    builder: (column) => ColumnOrderings(column),
  );

  $$BotRowsTableOrderingComposer get botId {
    final $$BotRowsTableOrderingComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.botRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$BotRowsTableOrderingComposer(
            $db: $db,
            $table: $db.botRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$MetaRowsTableAnnotationComposer
    extends Composer<_$ChatDatabase, $MetaRowsTable> {
  $$MetaRowsTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get name =>
      $composableBuilder(column: $table.name, builder: (column) => column);

  GeneratedColumn<Uint8List> get payload =>
      $composableBuilder(column: $table.payload, builder: (column) => column);

  $$BotRowsTableAnnotationComposer get botId {
    final $$BotRowsTableAnnotationComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.botRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$BotRowsTableAnnotationComposer(
            $db: $db,
            $table: $db.botRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$MetaRowsTableTableManager
    extends
        RootTableManager<
          _$ChatDatabase,
          $MetaRowsTable,
          MetaRow,
          $$MetaRowsTableFilterComposer,
          $$MetaRowsTableOrderingComposer,
          $$MetaRowsTableAnnotationComposer,
          $$MetaRowsTableCreateCompanionBuilder,
          $$MetaRowsTableUpdateCompanionBuilder,
          (MetaRow, $$MetaRowsTableReferences),
          MetaRow,
          PrefetchHooks Function({bool botId})
        > {
  $$MetaRowsTableTableManager(_$ChatDatabase db, $MetaRowsTable table)
    : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$MetaRowsTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$MetaRowsTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$MetaRowsTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<String> botId = const Value.absent(),
                Value<String> name = const Value.absent(),
                Value<Uint8List> payload = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => MetaRowsCompanion(
                botId: botId,
                name: name,
                payload: payload,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String botId,
                required String name,
                required Uint8List payload,
                Value<int> rowid = const Value.absent(),
              }) => MetaRowsCompanion.insert(
                botId: botId,
                name: name,
                payload: payload,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map(
                (e) => (
                  e.readTable<$MetaRowsTable, MetaRow>(table),
                  $$MetaRowsTableReferences(db, table, e),
                ),
              )
              .toList(),
          prefetchHooksCallback: ({botId = false}) {
            return PrefetchHooks(
              db: db,
              explicitlyWatchedTables: [],
              addJoins:
                  <
                    T extends TableManagerState<
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic
                    >
                  >(state) {
                    if (botId) {
                      state = state.withJoin(
                        currentTable: table,
                        currentColumn: table.botId,
                        referencedTable: $$MetaRowsTableReferences._botIdTable(
                          db,
                        ),
                        referencedColumn: $$MetaRowsTableReferences
                            ._botIdTable(db)
                            .botId,
                      ) as T;
                    }

                    return state;
                  },
              getPrefetchedDataCallback: (items) async {
                return [];
              },
            );
          },
        ),
      );
}

typedef $$MetaRowsTableProcessedTableManager =
    ProcessedTableManager<
      _$ChatDatabase,
      $MetaRowsTable,
      MetaRow,
      $$MetaRowsTableFilterComposer,
      $$MetaRowsTableOrderingComposer,
      $$MetaRowsTableAnnotationComposer,
      $$MetaRowsTableCreateCompanionBuilder,
      $$MetaRowsTableUpdateCompanionBuilder,
      (MetaRow, $$MetaRowsTableReferences),
      MetaRow,
      PrefetchHooks Function({bool botId})
    >;
typedef $$ViewedRepliesTableCreateCompanionBuilder =
    ViewedRepliesCompanion Function({
      required String botId,
      required String messageId,
      Value<int> rowid,
    });
typedef $$ViewedRepliesTableUpdateCompanionBuilder =
    ViewedRepliesCompanion Function({
      Value<String> botId,
      Value<String> messageId,
      Value<int> rowid,
    });

final class $$ViewedRepliesTableReferences
    extends BaseReferences<_$ChatDatabase, $ViewedRepliesTable, ViewedReply> {
  $$ViewedRepliesTableReferences(
    super.$_db,
    super.$_table,
    super.$_typedResult,
  );

  static $BotRowsTable _botIdTable(_$ChatDatabase db) =>
      db.botRows.createAlias('viewed_replies__bot_id__bot_rows__bot_id');

  $$BotRowsTableProcessedTableManager get botId {
    final $_column = $_itemColumn<String>('bot_id')!;

    final manager = $$BotRowsTableTableManager(
      $_db,
      $_db.botRows,
    ).filter((f) => f.botId.sqlEquals($_column));
    final item = $_typedResult.readTableOrNull(_botIdTable($_db));
    if (item == null) return manager;
    return ProcessedTableManager(
      manager.$state.copyWith(prefetchedData: [item]),
    );
  }
}

class $$ViewedRepliesTableFilterComposer
    extends Composer<_$ChatDatabase, $ViewedRepliesTable> {
  $$ViewedRepliesTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get messageId => $composableBuilder(
    column: $table.messageId,
    builder: (column) => ColumnFilters(column),
  );

  $$BotRowsTableFilterComposer get botId {
    final $$BotRowsTableFilterComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.botRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$BotRowsTableFilterComposer(
            $db: $db,
            $table: $db.botRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$ViewedRepliesTableOrderingComposer
    extends Composer<_$ChatDatabase, $ViewedRepliesTable> {
  $$ViewedRepliesTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get messageId => $composableBuilder(
    column: $table.messageId,
    builder: (column) => ColumnOrderings(column),
  );

  $$BotRowsTableOrderingComposer get botId {
    final $$BotRowsTableOrderingComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.botRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$BotRowsTableOrderingComposer(
            $db: $db,
            $table: $db.botRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$ViewedRepliesTableAnnotationComposer
    extends Composer<_$ChatDatabase, $ViewedRepliesTable> {
  $$ViewedRepliesTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get messageId =>
      $composableBuilder(column: $table.messageId, builder: (column) => column);

  $$BotRowsTableAnnotationComposer get botId {
    final $$BotRowsTableAnnotationComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.botId,
      referencedTable: $db.botRows,
      getReferencedColumn: (t) => t.botId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$BotRowsTableAnnotationComposer(
            $db: $db,
            $table: $db.botRows,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$ViewedRepliesTableTableManager
    extends
        RootTableManager<
          _$ChatDatabase,
          $ViewedRepliesTable,
          ViewedReply,
          $$ViewedRepliesTableFilterComposer,
          $$ViewedRepliesTableOrderingComposer,
          $$ViewedRepliesTableAnnotationComposer,
          $$ViewedRepliesTableCreateCompanionBuilder,
          $$ViewedRepliesTableUpdateCompanionBuilder,
          (ViewedReply, $$ViewedRepliesTableReferences),
          ViewedReply,
          PrefetchHooks Function({bool botId})
        > {
  $$ViewedRepliesTableTableManager(_$ChatDatabase db, $ViewedRepliesTable table)
    : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$ViewedRepliesTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$ViewedRepliesTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$ViewedRepliesTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<String> botId = const Value.absent(),
                Value<String> messageId = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => ViewedRepliesCompanion(
                botId: botId,
                messageId: messageId,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String botId,
                required String messageId,
                Value<int> rowid = const Value.absent(),
              }) => ViewedRepliesCompanion.insert(
                botId: botId,
                messageId: messageId,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map(
                (e) => (
                  e.readTable<$ViewedRepliesTable, ViewedReply>(table),
                  $$ViewedRepliesTableReferences(db, table, e),
                ),
              )
              .toList(),
          prefetchHooksCallback: ({botId = false}) {
            return PrefetchHooks(
              db: db,
              explicitlyWatchedTables: [],
              addJoins:
                  <
                    T extends TableManagerState<
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic
                    >
                  >(state) {
                    if (botId) {
                      state = state.withJoin(
                        currentTable: table,
                        currentColumn: table.botId,
                        referencedTable: $$ViewedRepliesTableReferences
                            ._botIdTable(db),
                        referencedColumn: $$ViewedRepliesTableReferences
                            ._botIdTable(db)
                            .botId,
                      ) as T;
                    }

                    return state;
                  },
              getPrefetchedDataCallback: (items) async {
                return [];
              },
            );
          },
        ),
      );
}

typedef $$ViewedRepliesTableProcessedTableManager =
    ProcessedTableManager<
      _$ChatDatabase,
      $ViewedRepliesTable,
      ViewedReply,
      $$ViewedRepliesTableFilterComposer,
      $$ViewedRepliesTableOrderingComposer,
      $$ViewedRepliesTableAnnotationComposer,
      $$ViewedRepliesTableCreateCompanionBuilder,
      $$ViewedRepliesTableUpdateCompanionBuilder,
      (ViewedReply, $$ViewedRepliesTableReferences),
      ViewedReply,
      PrefetchHooks Function({bool botId})
    >;

class $ChatDatabaseManager {
  final _$ChatDatabase _db;
  $ChatDatabaseManager(this._db);
  $$BotRowsTableTableManager get botRows =>
      $$BotRowsTableTableManager(_db, _db.botRows);
  $$TurnRowsTableTableManager get turnRows =>
      $$TurnRowsTableTableManager(_db, _db.turnRows);
  $$QueueRowsTableTableManager get queueRows =>
      $$QueueRowsTableTableManager(_db, _db.queueRows);
  $$MetaRowsTableTableManager get metaRows =>
      $$MetaRowsTableTableManager(_db, _db.metaRows);
  $$ViewedRepliesTableTableManager get viewedReplies =>
      $$ViewedRepliesTableTableManager(_db, _db.viewedReplies);
}
