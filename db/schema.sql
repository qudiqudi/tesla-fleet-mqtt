-- Relational schema for derived Tesla sessions (drives, charges, parks).
-- Apply once to your MariaDB:  mysql -h <host> -u root -p < db/schema.sql
-- Then create a least-privilege user (adjust password):
--   CREATE USER IF NOT EXISTS 'tesla'@'%' IDENTIFIED BY 'CHANGEME';
--   GRANT ALL PRIVILEGES ON tesla.* TO 'tesla'@'%';
--   FLUSH PRIVILEGES;

CREATE DATABASE IF NOT EXISTS tesla CHARACTER SET utf8mb4;
USE tesla;

-- One row per drive.
CREATE TABLE IF NOT EXISTS drives (
  id             BIGINT AUTO_INCREMENT PRIMARY KEY,
  vin            VARCHAR(20)  NOT NULL,
  start_ts       DATETIME     NOT NULL,
  end_ts         DATETIME     NULL,
  duration_s     INT          NULL,
  start_odometer DOUBLE       NULL,
  end_odometer   DOUBLE       NULL,
  distance_km    DOUBLE       NULL,
  start_soc      DOUBLE       NULL,
  end_soc        DOUBLE       NULL,
  soc_used       DOUBLE       NULL,
  start_lat      DOUBLE       NULL,
  start_lng      DOUBLE       NULL,
  end_lat        DOUBLE       NULL,
  end_lng        DOUBLE       NULL,
  max_speed      DOUBLE       NULL,
  avg_speed      DOUBLE       NULL,
  outside_temp   DOUBLE       NULL,
  source         VARCHAR(16)  NOT NULL DEFAULT 'live',   -- 'live' or 'backfill'
  UNIQUE KEY uniq_drive (vin, start_ts),
  KEY idx_drive_vin_start (vin, start_ts)
) ENGINE=InnoDB;

-- One row per charge session.
CREATE TABLE IF NOT EXISTS charges (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  vin           VARCHAR(20)  NOT NULL,
  start_ts      DATETIME     NOT NULL,
  end_ts        DATETIME     NULL,
  duration_s    INT          NULL,
  start_soc     DOUBLE       NULL,
  end_soc       DOUBLE       NULL,
  soc_added     DOUBLE       NULL,
  energy_added_kwh DOUBLE    NULL,
  max_power_kw  DOUBLE       NULL,
  charger_type  VARCHAR(8)   NULL,   -- 'AC' or 'DC'
  lat           DOUBLE       NULL,
  lng           DOUBLE       NULL,
  source        VARCHAR(16)  NOT NULL DEFAULT 'live',
  UNIQUE KEY uniq_charge (vin, start_ts),
  KEY idx_charge_vin_start (vin, start_ts)
) ENGINE=InnoDB;

-- One row per park (idle) period; used for vampire-drain analysis.
CREATE TABLE IF NOT EXISTS parks (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  vin         VARCHAR(20)  NOT NULL,
  start_ts    DATETIME     NOT NULL,
  end_ts      DATETIME     NULL,
  duration_s  INT          NULL,
  start_soc   DOUBLE       NULL,
  end_soc     DOUBLE       NULL,
  soc_loss    DOUBLE       NULL,
  lat         DOUBLE       NULL,
  lng         DOUBLE       NULL,
  source      VARCHAR(16)  NOT NULL DEFAULT 'live',
  UNIQUE KEY uniq_park (vin, start_ts),
  KEY idx_park_vin_start (vin, start_ts)
) ENGINE=InnoDB;
